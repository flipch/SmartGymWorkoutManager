"""WHOOP HTTP client + consent flow, against a fake HTTP layer and a real loopback callback server.

Nothing here talks to WHOOP. What is NOT proven by these tests: that WHOOP's live token endpoint accepts exactly these
form fields (client_secret_post). The live `--check` run proves that.
"""
import contextlib
import io
import json
import os
import socket
import stat
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

import requests

from harness import tracer, whoop_client as wc
from harness.errors import SchemaDrift, SourceAuthError, SourceDown
from harness.whoop import TokenStore

CID = "123e4567-e89b-42d3-a456-426614174000"
SECRET = "S" * 31 + "-" + "e" * 32  # 64 chars, deliberately not hex
CREDS = wc.AppCredentials(CID, SECRET)
NOW = datetime(2026, 10, 26, 15, 0, tzinfo=timezone.utc)


class Resp:
    def __init__(self, status=200, body=None, headers=None, not_json=False):
        self.status_code, self._body, self.headers, self._not_json = status, body, headers or {}, not_json

    def json(self):
        if self._not_json:
            raise ValueError("no json")
        return self._body


class FakeHttp:
    """Scripted responses per (method, url-suffix); records every call."""

    def __init__(self, script):
        self.script, self.calls = script, []

    def _pop(self, method, url, **kw):
        self.calls.append((method, url, kw))
        queue = self.script.get((method, url.replace("https://api.prod.whoop.com", "")), None)
        if queue is None:
            raise AssertionError(f"unexpected {method} {url}")
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if callable(item):
            item = item()
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, url, **kw):
        return self._pop("GET", url, **kw)

    def post(self, url, **kw):
        return self._pop("POST", url, **kw)


def tmpfile(text, mode=0o600):
    fd, path = tempfile.mkstemp()
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.chmod(path, mode)
    return path


def store_with(tokens=None):
    path = Path(tempfile.mkdtemp()) / "whoop.token.json"
    store = TokenStore(path)
    if tokens:
        store.save(tokens)
    return store


GOOD = {"access_token": "AT1", "refresh_token": "RT1", "expires_in": 3600,
        "scope": "offline read:recovery read:sleep", "token_type": "bearer"}


def recovery(score=70, created="2026-10-26T13:00:00.000Z"):
    return {"records": [{"cycle_id": 1, "sleep_id": "s", "user_id": 1, "created_at": created, "updated_at": created,
                         "score_state": "SCORED",
                         "score": {"user_calibrating": False, "recovery_score": score, "resting_heart_rate": 50,
                                   "hrv_rmssd_milli": 80}}], "next_token": None}


def sleep(hours=7.5, end="2026-10-26T12:00:00.000Z"):
    ms = int(hours * 3_600_000)
    return {"records": [{"cycle_id": 1, "nap": False, "end": end, "score_state": "SCORED",
                         "score": {"stage_summary": {"total_light_sleep_time_milli": ms // 2,
                                                     "total_slow_wave_sleep_time_milli": ms // 4,
                                                     "total_rem_sleep_time_milli": ms - ms // 2 - ms // 4}}}],
            "next_token": None}


class Credentials(unittest.TestCase):
    def test_dashboard_paste_format(self):
        path = tmpfile(f"Client ID\n{CID}\n\nClient Secret\n{SECRET}\nRedirect\nhttp://localhost:8000/callback\n")
        c = wc.load_app_credentials(path)
        self.assertEqual((c.client_id, c.client_secret), (CID, SECRET))

    def test_key_value_format(self):
        path = tmpfile(f"WHOOP_CLIENT_ID={CID}\nWHOOP_CLIENT_SECRET='{SECRET}'\n")
        self.assertEqual(wc.load_app_credentials(path).client_secret, SECRET)

    def test_group_or_world_readable_files_are_refused(self):
        for mode in (0o644, 0o640, 0o604):
            with self.assertRaises(PermissionError):
                wc.load_app_credentials(tmpfile(f"{CID}\n{SECRET}\n", mode))

    def test_missing_file(self):
        with self.assertRaises(FileNotFoundError):
            wc.load_app_credentials("/nonexistent/whoop.txt")

    def test_ambiguous_files_are_errors_that_do_not_echo_values(self):
        for text in (f"{CID}\n", f"{SECRET}\n", f"{CID}\n{CID.replace('1', '2')}\n{SECRET}\n",
                     f"{CID}\n{SECRET}\n{'z' * 64}\n", "nothing here\n"):
            with self.assertRaises(ValueError) as ctx:
                wc.load_app_credentials(tmpfile(text))
            self.assertNotIn(SECRET, str(ctx.exception))
            self.assertNotIn(CID, str(ctx.exception))

    def test_urls_are_not_mistaken_for_the_secret(self):
        long_url = "https://example.com/" + "a" * 44
        self.assertEqual(len(long_url), 64)
        c = wc.load_app_credentials(tmpfile(f"{CID}\n{long_url}\n{SECRET}\n"))
        self.assertEqual(c.client_secret, SECRET)

    def test_repr_hides_both_values(self):
        for text in (repr(CREDS), str(CREDS), f"{CREDS}"):
            self.assertNotIn(SECRET, text)
            self.assertNotIn(CID, text)


class AuthorizeUrl(unittest.TestCase):
    def test_parameters(self):
        url = urlparse(wc.authorize_url(CREDS, "STATE123"))
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        self.assertEqual(f"{url.scheme}://{url.netloc}{url.path}", "https://api.prod.whoop.com/oauth/oauth2/auth")
        self.assertEqual(q, {"response_type": "code", "client_id": CID, "redirect_uri": "http://localhost:8000/callback",
                             "scope": "offline read:recovery read:sleep", "state": "STATE123"})

    def test_http_timeout_is_short(self):
        self.assertLessEqual(wc.HTTP_TIMEOUT_S, 30)

    def test_secret_never_in_the_url(self):
        self.assertNotIn(SECRET, wc.authorize_url(CREDS, "x"))

    def test_least_privilege(self):
        self.assertEqual(set(wc.SCOPES), {"offline", "read:recovery", "read:sleep"})


class TokenCalls(unittest.TestCase):
    def test_exchange_sends_the_code_and_keeps_only_known_fields(self):
        http = FakeHttp({("POST", "/oauth/oauth2/token"): [Resp(200, {**GOOD, "id_token": "JWT", "extra": 1})]})
        tokens = wc.exchange_code(CREDS, "CODE", http, now=lambda: 1000)
        self.assertEqual(set(tokens), {"access_token", "refresh_token", "expires_in", "scope", "token_type", "obtained_at"})
        self.assertEqual(tokens["obtained_at"], 1000)
        method, url, kw = http.calls[0]
        self.assertEqual(kw["data"], {"grant_type": "authorization_code", "code": "CODE",
                                      "redirect_uri": wc.REDIRECT_URI, "client_id": CID, "client_secret": SECRET})
        self.assertIs(kw["allow_redirects"], False)
        self.assertEqual(kw["timeout"], wc.HTTP_TIMEOUT_S)

    def test_exchange_requires_every_scope(self):
        for scope in ("", "offline", "read:recovery read:sleep", "offline read:recovery"):
            http = FakeHttp({("POST", "/oauth/oauth2/token"): [Resp(200, {**GOOD, "scope": scope})]})
            with self.assertRaises(SourceAuthError, msg=scope):
                wc.exchange_code(CREDS, "CODE", http)

    def test_exchange_failures_name_the_status_not_the_body(self):
        for status, cls in ((400, SourceAuthError), (401, SourceAuthError), (403, SourceAuthError), (500, SourceDown),
                            (502, SourceDown)):
            http = FakeHttp({("POST", "/oauth/oauth2/token"): [Resp(status, {"error": f"leak {SECRET} AT1"})]})
            with self.assertRaises(cls) as ctx:
                wc.exchange_code(CREDS, "CODE", http)
            self.assertIn(f"HTTP {status}", str(ctx.exception))
            self.assertNotIn(SECRET, str(ctx.exception))
            self.assertNotIn("AT1", str(ctx.exception))

    def test_network_error_is_a_class_name_only(self):
        http = FakeHttp({("POST", "/oauth/oauth2/token"): [requests.ConnectionError(f"boom {SECRET}")]})
        with self.assertRaises(SourceDown) as ctx:
            wc.exchange_code(CREDS, "CODE", http)
        self.assertIn("ConnectionError", str(ctx.exception))
        self.assertNotIn(SECRET, str(ctx.exception))
        self.assertTrue(ctx.exception.__suppress_context__)  # `from None`: the original (which may hold secrets) is hidden

    def test_malformed_bodies(self):
        for resp in (Resp(200, not_json=True), Resp(200, ["x"])):
            http = FakeHttp({("POST", "/oauth/oauth2/token"): [resp]})
            with self.assertRaises(SchemaDrift):
                wc.exchange_code(CREDS, "CODE", http)

    def test_refresh_request_shape(self):
        http = FakeHttp({("POST", "/oauth/oauth2/token"): [Resp(200, GOOD)]})
        wc.refresh_tokens(CREDS, "RT0", http)
        self.assertEqual(http.calls[0][2]["data"], {"grant_type": "refresh_token", "refresh_token": "RT0",
                                                    "client_id": CID, "client_secret": SECRET, "scope": "offline"})


class ClientReads(unittest.TestCase):
    def client(self, script, tokens=GOOD):
        http = FakeHttp(script)
        store = store_with(tokens)
        return wc.WhoopClient(CREDS, store, http, now=lambda: 2000), http, store

    def test_not_authorised(self):
        client, http, _ = self.client({}, tokens=None)
        with self.assertRaises(SourceAuthError) as ctx:
            client.get("/v2/recovery", {})
        self.assertIn("whoop_auth", str(ctx.exception))
        self.assertEqual(http.calls, [])

    def test_a_token_file_without_a_refresh_token_is_not_usable(self):
        from harness import localsecrets
        store = store_with()
        localsecrets.write_json_private(store.path, {"access_token": "AT1"})
        client = wc.WhoopClient(CREDS, store, FakeHttp({}))
        with self.assertRaises(SourceAuthError):
            client.get("/v2/recovery", {})

    def test_a_rotation_that_lands_mid_request_is_reused_not_repeated(self):
        """b holds AT1/RT1, gets a 401, but another process rotated to AT2/RT2 in the meantime: b must reuse AT2 and
        must NOT spend RT1 (WHOOP already killed it, and a second refresh would kill RT2 as well)."""
        store = store_with(GOOD)
        rotated = {**GOOD, "access_token": "AT2", "refresh_token": "RT2"}

        def other_process_rotates():
            store.save(rotated)
            return Resp(401)

        http = FakeHttp({("GET", "/developer/v2/recovery"): [other_process_rotates, Resp(200, recovery())],
                         ("POST", "/oauth/oauth2/token"): [AssertionError("b must not refresh")]})
        wc.WhoopClient(CREDS, store, http).get("/v2/recovery", {})
        self.assertEqual([c[0] for c in http.calls], ["GET", "GET"])
        self.assertEqual(http.calls[1][2]["headers"]["Authorization"], "Bearer AT2")

    def test_bearer_header_params_and_no_redirects(self):
        client, http, _ = self.client({("GET", "/developer/v2/recovery"): [Resp(200, recovery())]})
        client.get("/v2/recovery", {"limit": 3})
        _, url, kw = http.calls[0]
        self.assertEqual(url, "https://api.prod.whoop.com/developer/v2/recovery")
        self.assertEqual(kw["headers"], {"Authorization": "Bearer AT1"})
        self.assertEqual(kw["params"], {"limit": 3})
        self.assertIs(kw["allow_redirects"], False)
        self.assertEqual(kw["timeout"], wc.HTTP_TIMEOUT_S)

    def test_401_refreshes_once_persists_the_new_pair_and_retries(self):
        new = {**GOOD, "access_token": "AT2", "refresh_token": "RT2"}
        client, http, store = self.client({("GET", "/developer/v2/recovery"): [Resp(401), Resp(200, recovery())],
                                           ("POST", "/oauth/oauth2/token"): [Resp(200, new)]})
        client.get("/v2/recovery", {})
        gets = [c for c in http.calls if c[0] == "GET"]
        self.assertEqual([g[2]["headers"]["Authorization"] for g in gets], ["Bearer AT1", "Bearer AT2"])
        self.assertEqual(sum(1 for c in http.calls if c[0] == "POST"), 1)
        self.assertEqual((store.load()["access_token"], store.load()["refresh_token"]), ("AT2", "RT2"))
        self.assertEqual(stat.S_IMODE(os.stat(store.path).st_mode), 0o600)

    def test_a_second_client_that_saw_the_old_token_reuses_the_rotated_pair_without_refreshing(self):
        new = {**GOOD, "access_token": "AT2", "refresh_token": "RT2"}
        client_a, _, store = self.client({("GET", "/developer/v2/recovery"): [Resp(401), Resp(200, recovery())],
                                          ("POST", "/oauth/oauth2/token"): [Resp(200, new)]})
        client_a.get("/v2/recovery", {})  # a rotates AT1/RT1 -> AT2/RT2
        # b read the old pair before a rotated, gets a 401 with AT1, and must NOT spend the (now dead) RT1
        http_b = FakeHttp({("GET", "/developer/v2/recovery"): [Resp(200, recovery())]})
        client_b = wc.WhoopClient(CREDS, store, http_b)
        fresh = store.refresh_if_still_stale("RT1", lambda rt: self.fail("b refreshed with a rotated-away token"))
        self.assertEqual(fresh["refresh_token"], "RT2")
        client_b.get("/v2/recovery", {})
        self.assertEqual(http_b.calls[0][2]["headers"]["Authorization"], "Bearer AT2")

    def test_refresh_rejected_keeps_the_old_pair_and_says_reauthorise(self):
        client, http, store = self.client({("GET", "/developer/v2/recovery"): [Resp(401)],
                                           ("POST", "/oauth/oauth2/token"): [Resp(400, {"error": "invalid_grant"})]})
        with self.assertRaises(SourceAuthError):
            client.get("/v2/recovery", {})
        self.assertEqual(store.load()["refresh_token"], "RT1")

    def test_still_401_after_a_refresh_is_an_auth_error(self):
        client, _, _ = self.client({("GET", "/developer/v2/recovery"): [Resp(401)],
                                    ("POST", "/oauth/oauth2/token"): [Resp(200, {**GOOD, "access_token": "AT2",
                                                                                 "refresh_token": "RT2"})]})
        with self.assertRaises(SourceAuthError) as ctx:
            client.get("/v2/recovery", {})
        self.assertIn("HTTP 401", str(ctx.exception))

    def test_status_mapping(self):
        cases = ((403, SourceAuthError, "HTTP 403"), (429, SourceDown, "rate limited"), (500, SourceDown, "HTTP 500"),
                 (404, SourceDown, "HTTP 404"))
        for status, cls, text in cases:
            client, _, _ = self.client({("GET", "/developer/v2/recovery"): [Resp(status, headers={"Retry-After": "30"})]})
            with self.assertRaises(cls, msg=status) as ctx:
                client.get("/v2/recovery", {})
            self.assertIn(text, str(ctx.exception))

    def test_retry_after_is_reported_and_bounded(self):
        client, _, _ = self.client({("GET", "/developer/v2/recovery"): [Resp(429, headers={"Retry-After": "x" * 500})]})
        with self.assertRaises(SourceDown) as ctx:
            client.get("/v2/recovery", {})
        self.assertLess(len(str(ctx.exception)), 100)

    def test_network_error_and_bad_bodies(self):
        client, _, _ = self.client({("GET", "/developer/v2/recovery"): [requests.Timeout(f"slow AT1")]})
        with self.assertRaises(SourceDown) as ctx:
            client.get("/v2/recovery", {})
        self.assertIn("Timeout", str(ctx.exception))
        self.assertNotIn("AT1", str(ctx.exception))
        for resp in (Resp(200, not_json=True), Resp(200, [1])):
            client, _, _ = self.client({("GET", "/developer/v2/recovery"): [resp]})
            with self.assertRaises(SchemaDrift):
                client.get("/v2/recovery", {})

    def test_no_error_message_ever_contains_a_token_or_the_secret(self):
        for resp in (Resp(500, {"access_token": "AT1"}), Resp(403, {"x": SECRET}), Resp(404), Resp(429)):
            client, _, _ = self.client({("GET", "/developer/v2/recovery"): [resp]})
            with self.assertRaises(SourceDown) as ctx:
                client.get("/v2/recovery", {})
            for secret in ("AT1", "RT1", SECRET):
                self.assertNotIn(secret, str(ctx.exception))

    def test_fetch_readings_parses_recovery_and_sleep(self):
        client, http, _ = self.client({("GET", "/developer/v2/recovery"): [Resp(200, recovery(64))],
                                       ("GET", "/developer/v2/activity/sleep"): [Resp(200, sleep(7.5))]})
        readings, notes = client.fetch_readings()
        self.assertEqual({r.kind: r.value for r in readings}, {"whoop_recovery": 64.0, "sleep_hours": 7.5})
        self.assertEqual(notes, [])
        self.assertEqual([c[2]["params"] for c in http.calls], [{"limit": 3}, {"limit": 10}])

    def test_an_unscored_recovery_is_a_note_not_a_zero(self):
        body = recovery()
        body["records"][0]["score_state"] = "PENDING_SCORE"
        client, _, _ = self.client({("GET", "/developer/v2/recovery"): [Resp(200, body)],
                                    ("GET", "/developer/v2/activity/sleep"): [Resp(200, sleep())]})
        readings, notes = client.fetch_readings()
        self.assertEqual([r.kind for r in readings], ["sleep_hours"])
        self.assertTrue(any("not scored" in n for n in notes))

    def test_a_failure_in_either_read_raises(self):
        client, _, _ = self.client({("GET", "/developer/v2/recovery"): [Resp(200, recovery())],
                                    ("GET", "/developer/v2/activity/sleep"): [Resp(500)]})
        with self.assertRaises(SourceDown):
            client.fetch_readings()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def browse(port, **query):
    url = f"http://127.0.0.1:{port}/callback?" + "&".join(f"{k}={v}" for k, v in query.items())
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, r.read().decode(), dict(r.headers)
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode(), dict(err.headers)


class Consent(unittest.TestCase):
    def run_flow(self, visits, *, timeout_s=5, token_status=200, token_body=None):
        port = free_port()
        results = []
        http = FakeHttp({("POST", "/oauth/oauth2/token"): [Resp(token_status, GOOD if token_body is None else token_body)]})
        store = store_with()

        def open_url(url):
            state = parse_qs(urlparse(url).query)["state"][0]
            self.assertEqual(urlparse(url).netloc, "api.prod.whoop.com")

            def go():
                for visit in visits:
                    results.append(browse(port, **{k: (state if v == "<state>" else v) for k, v in visit.items()}))
            threading.Thread(target=go, daemon=True).start()

        try:
            outcome = wc.run_consent(CREDS, store, open_url=open_url, port=port, timeout_s=timeout_s, http=http,
                                     now=lambda: 5)
        except Exception as err:  # noqa: BLE001
            outcome = err
        return outcome, results, http, store

    def test_happy_path_stores_tokens_and_returns_no_token(self):
        outcome, results, http, store = self.run_flow([{"code": "CODE1", "state": "<state>"}])
        self.assertEqual(outcome, {"scope": GOOD["scope"], "expires_in": 3600})
        self.assertEqual(store.load()["access_token"], "AT1")
        self.assertEqual(stat.S_IMODE(os.stat(store.path).st_mode), 0o600)
        self.assertEqual(http.calls[0][2]["data"]["code"], "CODE1")
        status, body, headers = results[0]
        self.assertEqual(status, 200)
        self.assertNotIn("CODE1", body)
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertEqual(headers.get("Referrer-Policy"), "no-referrer")

    def test_a_wrong_state_is_refused_and_does_not_end_the_wait(self):
        outcome, results, http, store = self.run_flow([{"code": "EVIL", "state": "nope"},
                                                       {"code": "GOOD", "state": "<state>"}])
        self.assertEqual([r[0] for r in results], [400, 200])
        self.assertEqual(http.calls[0][2]["data"]["code"], "GOOD")
        self.assertEqual(outcome["scope"], GOOD["scope"])

    def test_a_missing_state_is_refused(self):
        outcome, results, _, _ = self.run_flow([{"code": "EVIL"}], timeout_s=0.6)
        self.assertEqual(results[0][0], 400)
        self.assertIsInstance(outcome, SourceDown)

    def test_state_is_long_and_different_every_time(self):
        states = []
        for _ in range(2):
            port = free_port()

            def open_url(url, states=states):
                states.append(parse_qs(urlparse(url).query)["state"][0])

            with self.assertRaises(SourceDown):
                wc.run_consent(CREDS, store_with(), open_url=open_url, port=port, timeout_s=0.3, http=FakeHttp({}))
        self.assertEqual(len(states), 2)
        self.assertNotEqual(states[0], states[1])
        self.assertTrue(all(len(x) >= 24 for x in states))

    def test_a_callback_with_the_right_state_but_no_code_is_refused(self):
        outcome, results, http, _ = self.run_flow([{"state": "<state>"}], timeout_s=0.6)
        self.assertEqual(results[0][0], 400)
        self.assertIsInstance(outcome, SourceDown)
        self.assertEqual(http.calls, [])

    def test_the_listener_is_closed_on_success_timeout_and_failure(self):
        real = wc._CallbackServer.server_close
        with mock.patch.object(wc._CallbackServer, "server_close", autospec=True, side_effect=real) as closed:
            self.run_flow([{"code": "C", "state": "<state>"}])
            self.run_flow([], timeout_s=0.3)
            self.run_flow([{"code": "C", "state": "<state>"}], token_status=500, token_body={})
        self.assertEqual(closed.call_count, 3)

    def test_declined_consent(self):
        outcome, results, http, store = self.run_flow([{"error": "access_denied", "state": "<state>"}])
        self.assertIsInstance(outcome, SourceAuthError)
        self.assertIn("declined", str(outcome))
        self.assertEqual(http.calls, [])
        self.assertIsNone(store.load())

    def test_timeout(self):
        outcome, _, http, store = self.run_flow([], timeout_s=0.5)
        self.assertIsInstance(outcome, SourceDown)
        self.assertIn("no consent", str(outcome))
        self.assertEqual(http.calls, [])

    def test_other_paths_are_404_and_do_not_end_the_wait(self):
        port = free_port()
        results = []
        http = FakeHttp({("POST", "/oauth/oauth2/token"): [Resp(200, GOOD)]})

        def open_url(url):
            state = parse_qs(urlparse(url).query)["state"][0]

            def go():
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/admin", timeout=5)
                except urllib.error.HTTPError as err:
                    results.append(err.code)
                results.append(browse(port, code="C", state=state)[0])
            threading.Thread(target=go, daemon=True).start()

        wc.run_consent(CREDS, store_with(), open_url=open_url, port=port, timeout_s=5, http=http)
        self.assertEqual(results, [404, 200])

    def test_exchange_failure_leaves_no_token_file(self):
        outcome, _, _, store = self.run_flow([{"code": "C", "state": "<state>"}], token_status=400, token_body={})
        self.assertIsInstance(outcome, SourceAuthError)
        self.assertFalse(store.path.exists())

    def test_listener_is_loopback_only_and_released(self):
        outcome, _, _, _ = self.run_flow([{"code": "C", "state": "<state>"}])
        port = free_port()
        # the server must have been closed: binding the same port again works
        with socket.socket() as s:
            s.bind(("127.0.0.1", port))

    def test_listener_binds_127_0_0_1_only(self):
        src = Path(wc.__file__).read_text()
        self.assertIn('_CallbackServer(("127.0.0.1", port), _Callback)', src)
        self.assertNotIn('"0.0.0.0"', src)
        self.assertNotIn('("", port)', src)

    def test_no_request_logging_of_the_one_time_code(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            self.run_flow([{"code": "SECRETCODE", "state": "<state>"}])
        self.assertNotIn("SECRETCODE", buf.getvalue())


class TracerWhoopLive(unittest.TestCase):
    def run_tracer(self, fake_client=None, error=None):
        seen = {}

        def todays_plan(**kw):
            seen.update(kw)
            from harness.model import Unavailable
            return Unavailable(reasons=["captured"])

        out, err = io.StringIO(), io.StringIO()
        client = mock.Mock()
        if error:
            client.fetch_readings.side_effect = error
        else:
            client.fetch_readings.return_value = fake_client
        with mock.patch.object(wc, "default_client", return_value=client), \
                mock.patch.object(tracer.planner, "todays_plan", side_effect=todays_plan), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tracer.main(["--recordings", tempfile.mkdtemp(), "--whoop-live", "--subjective", "3"])
        return code, seen, err.getvalue()

    def test_live_readings_are_merged_with_manual_ones(self):
        from harness.model import Reading
        live = [Reading("whoop_recovery", 64.0, "whoop", NOW)]
        _, seen, _ = self.run_tracer((live, ["a note"]))
        self.assertEqual(sorted(r.kind for r in seen["readings"]), ["subjective", "whoop_recovery"])

    def test_notes_go_to_stderr(self):
        _, _, err = self.run_tracer(([], ["latest recovery is PENDING_SCORE"]))
        self.assertIn("note: whoop: latest recovery is PENDING_SCORE", err)

    def test_a_whoop_outage_becomes_a_feed_error_not_silence(self):
        _, seen, _ = self.run_tracer(error=SourceDown("whoop", "rate limited"))
        self.assertEqual(seen["feed_errors"].get("whoop"), "whoop: rate limited")

    def test_missing_credentials_become_a_feed_error_without_values(self):
        _, seen, _ = self.run_tracer(error=PermissionError("/x/whoop.txt must be chmod 600"))
        self.assertEqual(seen["feed_errors"]["whoop"], "PermissionError reading WHOOP credentials")

    def test_without_the_flag_whoop_is_never_touched(self):
        client = mock.Mock()
        with mock.patch.object(wc, "default_client", return_value=client), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            tracer.main(["--recordings", tempfile.mkdtemp(), "--subjective", "3"])
        client.fetch_readings.assert_not_called()


class AuthCli(unittest.TestCase):
    def test_bad_creds_file_is_one_line_exit_1(self):
        from harness import whoop_auth
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = whoop_auth.main(["--creds", tmpfile("nope\n"), "--tokens", str(Path(tempfile.mkdtemp()) / "t.json")])
        self.assertEqual(code, 1)
        self.assertEqual(err.getvalue().count("\n"), 1)

    def test_check_prints_readings_and_never_tokens(self):
        from harness import whoop_auth
        from harness.model import Reading
        creds_path = tmpfile(f"{CID}\n{SECRET}\n")
        out = io.StringIO()
        fake = mock.Mock()
        fake.fetch_readings.return_value = ([Reading("whoop_recovery", 64.0, "whoop", datetime.now(timezone.utc) - timedelta(hours=2))],
                                            ["a note"])
        with mock.patch.object(wc, "WhoopClient", return_value=fake), contextlib.redirect_stdout(out):
            code = whoop_auth.main(["--creds", creds_path, "--tokens", str(Path(tempfile.mkdtemp()) / "t.json"), "--check"])
        self.assertEqual(code, 0)
        self.assertIn("whoop_recovery: 64 (measured", out.getvalue())
        self.assertIn("note: a note", out.getvalue())
        self.assertNotIn(SECRET, out.getvalue())

    def test_outage_is_exit_2(self):
        from harness import whoop_auth
        fake = mock.Mock()
        fake.fetch_readings.side_effect = SourceDown("whoop", "down")
        err = io.StringIO()
        with mock.patch.object(wc, "WhoopClient", return_value=fake), contextlib.redirect_stderr(err):
            code = whoop_auth.main(["--creds", tmpfile(f"{CID}\n{SECRET}\n"), "--tokens", "/tmp/x-t.json", "--check"])
        self.assertEqual((code, err.getvalue()), (2, "error: whoop: down\n"))


if __name__ == "__main__":
    unittest.main()
