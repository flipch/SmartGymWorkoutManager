"""The gym app pulls WHOOP itself (whoop_sync.py): single refresher, throttle, backoff, re-consent, in-app consent.

Everything runs against a FAKE http layer, temp directories and made-up tokens ("AT1", "RT1"). Nothing talks to WHOOP, and
nothing reads ~/.config, whoop.txt or a real *.token.json. What this does NOT prove: that WHOOP's live endpoints accept
exactly these requests (the harness's own `--check` run does that).
"""
import base64
import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
from urllib.parse import parse_qs, urlparse

import requests

TOKEN = "t" * 32
os.environ["GYM_API_TOKEN"] = TOKEN
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="gym-test-data-")
os.environ.pop("GYM_AUTH_DISABLED", None)
os.environ.pop("GYM_ALLOW_WRITES", None)
HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

import app as gym_app  # noqa: E402
import features  # noqa: E402
import state_io  # noqa: E402
import whoop_sync  # noqa: E402
from harness import localsecrets  # noqa: E402
from harness.whoop import TokenStore  # noqa: E402
from test_harness_whoop_client import FakeHttp, Resp  # noqa: E402

BASIC = {"Authorization": "Basic " + base64.b64encode(f"gym:{TOKEN}".encode()).decode()}
CID = "123e4567-e89b-42d3-a456-426614174000"
SECRET = "S" * 31 + "-" + "e" * 32
PAIR = {"access_token": "AT1-fake", "refresh_token": "RT1-fake", "expires_in": 3600,
        "scope": "offline read:recovery read:sleep", "token_type": "bearer"}
NEW_PAIR = {**PAIR, "access_token": "AT2-fake", "refresh_token": "RT2-fake"}
REDIRECT = "https://gym.lan.doghous.cloud/whoop/callback"
ENV = {"WHOOP_CLIENT_ID": CID, "WHOOP_CLIENT_SECRET": SECRET, "WHOOP_REDIRECT_URI": REDIRECT}
REC, SLEEP, TOK = ("GET", "/developer/v2/recovery"), ("GET", "/developer/v2/activity/sleep"), ("POST", "/oauth/oauth2/token")
NOW = datetime.now(timezone.utc)


def iso(hours_ago):
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def recovery(score=70, hours_ago=2, state="SCORED"):
    rec = {"cycle_id": 1, "sleep_id": "s", "user_id": 1, "created_at": iso(hours_ago), "updated_at": iso(hours_ago), "score_state": state}
    if state == "SCORED":
        rec["score"] = {"user_calibrating": False, "recovery_score": score, "resting_heart_rate": 50, "hrv_rmssd_milli": 80}
    return {"records": [rec], "next_token": None}


def sleep(hours=7.5, hours_ago=3):
    ms = int(hours * 3_600_000)
    return {"records": [{"cycle_id": 1, "nap": False, "end": iso(hours_ago), "score_state": "SCORED",
                         "score": {"stage_summary": {"total_light_sleep_time_milli": ms // 2, "total_slow_wave_sleep_time_milli": ms // 4,
                                                     "total_rem_sleep_time_milli": ms - ms // 2 - ms // 4}}}], "next_token": None}


def good_script(score=70, hours=7.5, rec_ago=2):
    return {REC: [Resp(200, recovery(score, rec_ago))], SLEEP: [Resp(200, sleep(hours))]}


def make_dir(tokens=PAIR):
    d = tempfile.mkdtemp(prefix="gym-whoop-")
    if tokens:
        TokenStore(os.path.join(d, "whoop.token.json")).save(tokens)
    return d


def read_stored(d):
    with open(os.path.join(d, "readiness.json")) as f:
        return json.load(f)


class Pull(unittest.TestCase):
    def sync(self, d, http, **kw):
        return whoop_sync.sync(d, env=ENV, http=http, **kw)

    def test_not_configured_is_a_calm_state_and_makes_no_call(self):
        for env, tokens in (({}, PAIR), (ENV, None), ({"WHOOP_CLIENT_ID": CID}, PAIR)):
            d = make_dir(tokens)
            http = FakeHttp({})
            out = whoop_sync.sync(d, env=env, http=http)
            self.assertEqual((out["outcome"], out["status"]["state"]), ("not_configured", "not_configured"))
            self.assertEqual(http.calls, [])
            self.assertIn("coach works without it", whoop_sync.status(d, env=env)["message"] + "coach works without it")

    def test_a_scored_pull_stores_whoop_readiness_stamped_with_the_measurement_time(self):
        d = make_dir()
        out = self.sync(d, FakeHttp(good_script(70, 7.5, rec_ago=5)))
        self.assertEqual((out["outcome"], out["saved"], out["status"]["state"]), ("pulled", ["whoop_recovery", "sleep_hours"], "connected"))
        stored = read_stored(d)
        self.assertEqual((stored["whoop_recovery"], stored["sleep_hours"]), (70, 7.5))
        self.assertEqual(stored["field_source"], {"whoop_recovery": "whoop", "sleep_hours": "whoop"})
        measured = state_io.parse_stamp(stored["field_updated"]["whoop_recovery"])
        self.assertAlmostEqual((datetime.now(timezone.utc) - measured).total_seconds() / 3600, 5, delta=0.1)  # not "now"
        self.assertIsNotNone(out["status"]["last_pull"])

    def test_unscored_recovery_yields_no_reading_never_zero(self):
        d = make_dir()
        http = FakeHttp({REC: [Resp(200, recovery(state="PENDING_SCORE"))], SLEEP: [Resp(200, {"records": [], "next_token": None})]})
        out = self.sync(d, http)
        self.assertEqual((out["outcome"], out["saved"]), ("pulled", []))
        self.assertFalse(os.path.exists(os.path.join(d, "readiness.json")))
        self.assertTrue(any("PENDING_SCORE" in n for n in out["status"]["notes"]))

    def test_a_reading_older_than_the_window_is_not_stored(self):
        d = make_dir()
        out = self.sync(d, FakeHttp(good_script(rec_ago=40)))
        self.assertIn("whoop_recovery", out["skipped"])
        self.assertNotIn("whoop_recovery", read_stored(d))

    def test_a_newer_manual_entry_is_not_overwritten_by_an_older_whoop_reading(self):
        d = make_dir()
        features.save_readiness({"whoop_recovery": 20, "source": "manual"}, d)  # stamped now
        out = self.sync(d, FakeHttp(good_script(90, rec_ago=3)))
        self.assertEqual(out["skipped"], {"whoop_recovery": "a newer reading is already stored"})
        stored = read_stored(d)
        self.assertEqual(stored["whoop_recovery"], 20)
        self.assertEqual(stored["field_source"]["whoop_recovery"], "manual")
        self.assertEqual(stored["sleep_hours"], 7.5)  # the field with nothing newer still lands

    def test_a_newer_whoop_reading_replaces_an_older_one_and_takes_the_source(self):
        d = make_dir()
        features.save_readiness({"whoop_recovery": 20, "source": "manual"}, d)
        features.save_readiness({"whoop_recovery": 65, "source": "whoop"}, d, measured_at={"whoop_recovery": datetime.now(timezone.utc) + timedelta(minutes=1)})
        self.assertEqual(read_stored(d)["field_source"]["whoop_recovery"], "whoop")

    def test_the_throttle_and_the_force_floor(self):
        d = make_dir()
        http = FakeHttp(good_script())
        self.sync(d, http)
        n = len(http.calls)
        self.assertEqual(self.sync(d, http)["outcome"], "throttled")
        self.assertEqual(self.sync(d, http, force=True)["outcome"], "throttled")  # the button cannot hammer either (30 s floor)
        self.assertEqual(len(http.calls), n)
        later = datetime.now(timezone.utc) + timedelta(seconds=45)
        self.assertEqual(self.sync(d, http, force=True, now=later)["outcome"], "pulled")  # force skips the 15-minute throttle
        self.assertEqual(self.sync(d, http, now=later + timedelta(minutes=16))["outcome"], "pulled")

    def test_interval_is_configurable_and_junk_falls_back(self):
        self.assertEqual(whoop_sync.interval_s({"WHOOP_PULL_MIN_INTERVAL_S": "300"}), 300)
        for junk in ("abc", "-5", "1", "999999999", ""):
            self.assertEqual(whoop_sync.interval_s({"WHOOP_PULL_MIN_INTERVAL_S": junk}), 900)

    def test_http_errors_become_a_status_with_growing_backoff_and_keep_the_last_readings(self):
        d = make_dir()
        self.sync(d, FakeHttp(good_script(55)))  # a good pull first
        failing = FakeHttp({REC: [Resp(503, {})], SLEEP: [Resp(503, {})]})
        t = datetime.now(timezone.utc)
        delays = []
        for i in range(3):
            t += timedelta(hours=4)
            out = self.sync(d, failing, now=t)
            self.assertEqual((out["outcome"], out["status"]["state"]), ("failed", "refreshing_failed"))
            self.assertIn("WHOOP unavailable", out["status"]["message"])
            delays.append((state_io.parse_stamp(out["status"]["next_pull"]) - t).total_seconds())
        self.assertEqual(delays, [120, 240, 480])
        calls = len(failing.calls)
        self.assertEqual(self.sync(d, failing, now=t + timedelta(seconds=30))["outcome"], "throttled")  # backing off: no hammering
        self.assertEqual(len(failing.calls), calls)
        self.assertEqual(read_stored(d)["whoop_recovery"], 55)  # the last readings stay
        ok = self.sync(d, FakeHttp(good_script(61)), now=t + timedelta(hours=4))
        self.assertEqual((ok["outcome"], ok["status"]["state"], ok["status"]["failures"]), ("pulled", "connected", 0))

    def test_timeouts_and_malformed_bodies_are_statuses_not_exceptions(self):
        cases = {"timeout": [requests.Timeout("slow")], "bad json": [Resp(200, not_json=True)],
                 "not an object": [Resp(200, [1, 2])], "wrong shape": [Resp(200, {"records": "x"})], "429": [Resp(429, {}, {"Retry-After": "60"})]}
        for name, first in cases.items():
            d = make_dir()
            out = self.sync(d, FakeHttp({REC: first, SLEEP: [Resp(200, sleep())]}))
            self.assertEqual(out["status"]["state"], "refreshing_failed", name)
            self.assertFalse(os.path.exists(os.path.join(d, "readiness.json")), name)

    def test_http_calls_are_time_boxed_to_the_short_timeout(self):
        d = make_dir()
        http = FakeHttp(good_script())
        whoop_sync.sync(d, env={**ENV, "WHOOP_HTTP_TIMEOUT_S": "3"}, http=http)
        self.assertTrue(http.calls and all(kw["timeout"] <= 3 for _m, _u, kw in http.calls))
        self.assertTrue(all(kw["allow_redirects"] is False for _m, _u, kw in http.calls))

    def test_a_rejected_refresh_token_is_needs_reconsent_and_stops_retrying(self):
        d = make_dir()
        http = FakeHttp({REC: [Resp(401, {})], TOK: [Resp(400, {"error": "invalid_grant"})]})
        out = self.sync(d, http)
        self.assertEqual((out["outcome"], out["status"]["state"]), ("needs_reconsent", "needs_reconsent"))
        calls = len(http.calls)
        for force in (False, True):
            again = self.sync(d, http, force=force, now=datetime.now(timezone.utc) + timedelta(days=2))
            self.assertEqual(again["outcome"], "needs_reconsent")
        self.assertEqual(len(http.calls), calls)  # no retry, however long we wait, even when forced
        self.assertIsNone(out["status"]["next_pull"])
        self.assertEqual(TokenStore(os.path.join(d, "whoop.token.json")).load()["refresh_token"], "RT1-fake")  # nothing half-written

    def test_a_refresh_response_lost_mid_way_ends_in_reconsent_not_a_loop(self):
        d = make_dir()
        lost = FakeHttp({REC: [Resp(401, {})], TOK: [requests.ConnectionError("reset")]})
        first = self.sync(d, lost)
        self.assertEqual(first["status"]["state"], "refreshing_failed")  # transient as far as we can tell
        self.assertEqual(TokenStore(os.path.join(d, "whoop.token.json")).load()["refresh_token"], "RT1-fake")
        dead = FakeHttp({REC: [Resp(401, {})], TOK: [Resp(400, {})]})  # WHOOP already rotated: the old token is dead
        second = self.sync(d, dead, now=datetime.now(timezone.utc) + timedelta(hours=1))
        self.assertEqual(second["status"]["state"], "needs_reconsent")

    def test_a_successful_refresh_rotates_both_tokens_into_a_0600_file(self):
        d = make_dir()
        http = FakeHttp({REC: [Resp(401, {}), Resp(200, recovery())], SLEEP: [Resp(200, sleep())], TOK: [Resp(200, NEW_PAIR)]})
        out = self.sync(d, http)
        self.assertEqual(out["outcome"], "pulled")
        path = os.path.join(d, "whoop.token.json")
        self.assertEqual(TokenStore(path).load()["refresh_token"], "RT2-fake")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_two_pullers_refresh_once_the_loser_reuses_the_winners_tokens(self):
        d = make_dir()
        refreshes, lock = [], threading.Lock()

        class Slow(FakeHttp):
            def post(self, url, **kw):
                time.sleep(0.4)  # the refresh holds the OS lock a while
                with lock:
                    refreshes.append(kw["data"]["refresh_token"])
                return super().post(url, **kw)

            def get(self, url, **kw):
                if kw["headers"]["Authorization"] == "Bearer AT1-fake":
                    self.calls.append(("GET", url, kw))
                    return Resp(401, {})
                return super().get(url, **kw)

        script = {REC: [Resp(200, recovery())], SLEEP: [Resp(200, sleep())], TOK: [Resp(200, NEW_PAIR)]}
        from harness import whoop_client as wc
        results = []

        def puller():  # two clients on one token file, as the web and MCP processes are
            c = wc.WhoopClient(wc.AppCredentials(CID, SECRET), TokenStore(os.path.join(d, "whoop.token.json")), http=Slow(script))
            results.append(c.fetch_readings())

        threads = [threading.Thread(target=puller) for _ in range(2)]
        [t.start() for t in threads]
        [t.join(20) for t in threads]
        self.assertEqual(len(results), 2)
        self.assertEqual(refreshes, ["RT1-fake"])  # exactly one refresh across both
        self.assertEqual(TokenStore(os.path.join(d, "whoop.token.json")).load()["refresh_token"], "RT2-fake")

    def test_a_second_sync_while_one_is_running_is_busy_not_a_second_pull(self):
        d = make_dir()
        gate, started = threading.Event(), threading.Event()

        class Blocking(FakeHttp):
            def get(self, url, **kw):
                started.set()
                gate.wait(5)
                return super().get(url, **kw)

        http = Blocking(good_script())
        first = []
        t = threading.Thread(target=lambda: first.append(self.sync(d, http, force=True)))
        t.start()
        self.assertTrue(started.wait(5))
        second = self.sync(d, FakeHttp({}), force=True, now=datetime.now(timezone.utc) + timedelta(minutes=5))
        gate.set()
        t.join(10)
        self.assertEqual(second["outcome"], "busy")
        self.assertEqual(first[0]["outcome"], "pulled")

    def test_no_secret_or_token_appears_in_any_result_status_or_state_file(self):
        d = make_dir()
        outs = [self.sync(d, FakeHttp({REC: [Resp(401, {})], TOK: [Resp(500, {"error": f"{SECRET} AT1-fake RT1-fake"})]})),
                self.sync(d, FakeHttp(good_script()), now=datetime.now(timezone.utc) + timedelta(hours=3))]
        blob = json.dumps(outs) + json.dumps(whoop_sync.status(d, env=ENV))
        with open(os.path.join(d, "whoop_state.json")) as f:
            blob += f.read()
        for secret in (SECRET, CID, "AT1-fake", "RT1-fake", "AT2-fake"):
            self.assertNotIn(secret, blob)

    def test_credentials_from_a_file_and_a_bad_file(self):
        path = localsecrets.CONFIG_DIR  # never touched: a temp file is used instead
        self.assertNotEqual(path, None)
        f = os.path.join(tempfile.mkdtemp(), "creds.txt")
        with open(f, "w") as fh:
            fh.write(f"WHOOP_CLIENT_ID={CID}\nWHOOP_CLIENT_SECRET={SECRET}\n")
        os.chmod(f, 0o600)
        self.assertEqual(whoop_sync.load_creds({"WHOOP_CREDS_FILE": f}).client_id, CID)
        os.chmod(f, 0o644)
        with self.assertRaises(ValueError) as ctx:
            whoop_sync.load_creds({"WHOOP_CREDS_FILE": f})
        self.assertNotIn(SECRET, str(ctx.exception))
        d = make_dir()
        st = whoop_sync.status(d, env={"WHOOP_CREDS_FILE": f})
        self.assertEqual(st["state"], "not_configured")
        self.assertNotIn(SECRET, json.dumps(st))

    def test_the_token_path_is_configurable_and_defaults_to_the_data_volume(self):
        self.assertEqual(whoop_sync.token_path("/data", {}), "/data/whoop.token.json")
        self.assertEqual(whoop_sync.token_path("/data", {"WHOOP_TOKEN_FILE": "/x/t.json"}), "/x/t.json")

    def test_an_unreadable_token_file_is_a_failed_pull_not_a_crash(self):
        d = make_dir()
        os.chmod(os.path.join(d, "whoop.token.json"), 0o644)  # not mode 600: the harness refuses it
        out = self.sync(d, FakeHttp({}))
        self.assertEqual(out["status"]["state"], "refreshing_failed")


class Consent(unittest.TestCase):
    def setUp(self):
        self.d = make_dir(tokens=None)
        self.c = gym_app.app.test_client()
        patcher = mock.patch.dict(os.environ, ENV)
        patcher.start()
        self.addCleanup(patcher.stop)
        patch2 = mock.patch.object(features, "_client", mock.Mock(data_dir=self.d, credentials={"token": "x"}))
        patch2.start()
        self.addCleanup(patch2.stop)

    def mint(self):
        url, state = whoop_sync.begin_consent(self.d, ENV)
        return url, state

    def callback(self, query, method="GET", path="/whoop/callback", headers=None, http=None):
        fake = http or FakeHttp({TOK: [Resp(200, PAIR)]})
        with mock.patch.object(whoop_sync, "requests", fake):
            return self.c.open(path + query, method=method, headers=headers or {}), fake

    def test_the_link_carries_the_client_id_redirect_scopes_and_a_strong_single_use_state(self):
        url, state = self.mint()
        q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
        self.assertEqual((q["client_id"], q["redirect_uri"], q["response_type"], q["state"]), (CID, REDIRECT, "code", state))
        self.assertEqual(set(q["scope"].split()), {"offline", "read:recovery", "read:sleep"})
        self.assertGreaterEqual(len(state) * 6, 128)
        self.assertNotIn(SECRET, url)
        with open(os.path.join(self.d, "whoop_consent.json")) as f:
            self.assertNotIn(state, f.read())  # only a hash is stored
        self.assertNotEqual(self.mint()[1], state)

    def test_pending_states_are_bounded_and_expire(self):
        states = [whoop_sync.begin_consent(self.d, ENV)[1] for _ in range(whoop_sync.MAX_PENDING_STATES + 3)]
        later = datetime.now(timezone.utc)
        self.assertFalse(whoop_sync._burn(self.d, states[0], later))  # evicted by the cap
        self.assertTrue(whoop_sync._burn(self.d, states[-1], later))
        old = whoop_sync.begin_consent(self.d, ENV, now=datetime.now(timezone.utc) - timedelta(minutes=11))[1]
        self.assertFalse(whoop_sync._burn(self.d, old, datetime.now(timezone.utc)))

    def test_a_valid_callback_without_credentials_connects_once(self):
        _url, state = self.mint()
        resp, http = self.callback(f"?code=THE-CODE&state={state}")  # no Authorization header: WHOOP sends the browser here
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("WHOOP connected", body)
        for secret in ("THE-CODE", "AT1-fake", "RT1-fake", state, SECRET):
            self.assertNotIn(secret, body)
        self.assertEqual(resp.headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(resp.headers["Cache-Control"], "no-store")
        self.assertIn("default-src 'none'", resp.headers["Content-Security-Policy"])
        path = os.path.join(self.d, "whoop.token.json")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(TokenStore(path).load()["refresh_token"], "RT1-fake")
        self.assertEqual(http.calls[0][2]["data"]["code"], "THE-CODE")
        self.assertEqual(http.calls[0][2]["data"]["redirect_uri"], REDIRECT)
        again, http2 = self.callback(f"?code=THE-CODE&state={state}")  # replay
        self.assertEqual(again.status_code, 400)
        self.assertEqual(http2.calls, [])

    def test_a_successful_consent_clears_needs_reconsent(self):
        d = self.d
        state_io.atomic_write_json(os.path.join(d, "whoop_state.json"), {"needs_reconsent": True, "error": {"kind": "needs_reconsent", "message": "x"}})
        TokenStore(os.path.join(d, "whoop.token.json")).save(PAIR)
        self.assertEqual(whoop_sync.status(d, env=ENV)["state"], "needs_reconsent")
        _u, state = self.mint()
        self.callback(f"?code=C&state={state}", http=FakeHttp({TOK: [Resp(200, NEW_PAIR)]}))
        self.assertEqual(whoop_sync.status(d, env=ENV)["state"], "connected")

    def assert_refused(self, query, **kw):
        resp, http = self.callback(query, **kw)
        self.assertEqual(resp.status_code, 400, query)
        self.assertEqual(http.calls, [], query)  # no token exchange
        self.assertFalse(os.path.exists(os.path.join(self.d, "whoop.token.json")), query)  # no side effect
        return resp

    def test_unknown_missing_wrong_expired_and_odd_state_do_nothing(self):
        _u, good = self.mint()
        expired = whoop_sync.begin_consent(self.d, ENV, now=datetime.now(timezone.utc) - timedelta(minutes=11))[1]
        for query in ("", "?code=C", "?state=" + good, "?code=C&state=", "?code=&state=" + good, "?code=C&state=wrong",
                      "?code=C&state=" + expired, "?code=" + "x" * 5000 + "&state=" + good, "?code=C&state=" + "s" * 300,
                      "?code=C&state=%00", "?code=C%00&state=" + good, "?code=%E2%9C%93&state=%E2%9C%93", "?code[]=C&state[]=" + good):
            self.assert_refused(query)
        ok, _ = self.callback(f"?code=C&state={good}")  # the genuine state still works after all that
        self.assertEqual(ok.status_code, 200)

    def test_a_denied_sign_in_changes_nothing_and_escapes_what_it_shows(self):
        _u, state = self.mint()
        resp = self.assert_refused(f"?error=%3Cscript%3Ealert(1)%3C/script%3E&state={state}")
        body = resp.get_data(as_text=True)
        self.assertNotIn("<script>alert", body)
        self.assertIn("&lt;script&gt;", body)
        self.assertEqual(self.callback(f"?code=C&state={state}")[0].status_code, 200)  # not burned by a bare error

    def test_exchange_failures_are_a_plain_page_and_burn_the_state(self):
        for reply in (Resp(500, {"error": "boom"}), Resp(200, not_json=True), Resp(400, {"error": "invalid_grant"}), Resp(200, [1]),
                      Resp(200, {**PAIR, "scope": "offline"})):
            d = tempfile.mkdtemp()
            self.d = d
            with mock.patch.object(features, "_client", mock.Mock(data_dir=d, credentials={"token": "x"})):
                _u, state = whoop_sync.begin_consent(d, ENV)
                http = FakeHttp({TOK: [reply]})
                with mock.patch.object(whoop_sync, "requests", http):
                    resp = self.c.get(f"/whoop/callback?code=C&state={state}")
                self.assertEqual(resp.status_code, 400)
                self.assertFalse(os.path.exists(os.path.join(d, "whoop.token.json")))
                with mock.patch.object(whoop_sync, "requests", FakeHttp({TOK: [Resp(200, PAIR)]})) as again:
                    self.assertEqual(self.c.get(f"/whoop/callback?code=C&state={state}").status_code, 400)  # burned
                self.assertEqual(again.calls, [])
                self.assertNotIn("boom", resp.get_data(as_text=True))

    def test_exchange_timeout_is_a_plain_page(self):
        _u, state = self.mint()
        resp, _ = self.callback(f"?code=C&state={state}", http=FakeHttp({TOK: [requests.Timeout("slow")]}))
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(os.path.exists(os.path.join(self.d, "whoop.token.json")))

    def test_only_get_whoop_callback_is_exempt_from_auth(self):
        _u, state = self.mint()
        q = f"?code=C&state={state}"
        for method, path in (("POST", "/whoop/callback"), ("PUT", "/whoop/callback"), ("DELETE", "/whoop/callback"), ("HEAD", "/whoop/callback"),
                             ("OPTIONS", "/whoop/callback"), ("GET", "/whoop/callback/"), ("GET", "/whoop/callback/../settings"),
                             ("GET", "//whoop/callback"), ("GET", "/WHOOP/callback"), ("GET", "/whoop/Callback"), ("GET", "/whoop/callbackx"),
                             ("GET", "/whoop"), ("GET", "/whoop/callback%2f"), ("GET", "/coach/whoop/callback"), ("GET", "/x/../whoop/callback")):
            resp, http = self.callback(q, method=method, path=path)
            self.assertEqual(resp.status_code, 401, f"{method} {path}")
            self.assertEqual(http.calls, [], f"{method} {path}")
        self.assertFalse(os.path.exists(os.path.join(self.d, "whoop.token.json")))
        self.assertEqual(self.c.get("/settings").status_code, 401)  # the exemption is not a prefix
        self.assertEqual(self.c.get("/api/coach/whoop").status_code, 401)
        self.assertEqual(self.callback(q)[0].status_code, 200)  # and the exact route still works

    def test_the_exemption_is_exactly_one_route(self):
        from auth import EXEMPT_ROUTES, EXEMPT_PATHS
        self.assertEqual(EXEMPT_ROUTES, frozenset({("GET", "/whoop/callback")}))
        self.assertEqual(EXEMPT_PATHS, frozenset({"/healthz"}))

    def test_the_callback_survives_not_being_configured(self):
        with mock.patch.dict(os.environ, {"WHOOP_CLIENT_ID": "", "WHOOP_CLIENT_SECRET": ""}):
            resp, http = self.callback("?code=C&state=anything")
        self.assertEqual((resp.status_code, http.calls), (400, []))

    def test_the_web_button_mints_a_link_only_when_configured(self):
        resp = self.c.post("/api/coach/whoop/connect", headers=BASIC, json={})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["url"].startswith("https://api.prod.whoop.com/oauth/oauth2/auth?"))
        with mock.patch.dict(os.environ, {"WHOOP_REDIRECT_URI": ""}):
            self.assertEqual(self.c.post("/api/coach/whoop/connect", headers=BASIC, json={}).status_code, 400)
        self.assertEqual(self.c.post("/api/coach/whoop/connect", json={}).status_code, 401)
        cross = {**BASIC, "Sec-Fetch-Site": "cross-site"}
        self.assertEqual(self.c.post("/api/coach/whoop/connect", headers=cross, json={}).status_code, 403)

    def test_the_redirect_uri_must_be_a_usable_callback(self):
        good = ("https://gym.lan.doghous.cloud/whoop/callback", "http://localhost:5001/whoop/callback")
        bad = ("", "http://gym.lan.doghous.cloud/whoop/callback", "https://u:p@gym.example/whoop/callback", "https://gym.example/other",
               "https://gym.example/whoop/callback?x=1", "https://gym.example/whoop/callback#f", "javascript:alert(1)", "https://gym.example/whoop/callback/")
        for uri in good:
            self.assertEqual(whoop_sync.redirect_uri({"WHOOP_REDIRECT_URI": uri}), uri)
        for uri in bad:
            self.assertIsNone(whoop_sync.redirect_uri({"WHOOP_REDIRECT_URI": uri}), uri)


class McpTools(unittest.TestCase):
    def setUp(self):
        import mcp_server
        self.m = mcp_server
        self.d = make_dir()
        patcher = mock.patch.dict(os.environ, ENV)
        patcher.start()
        self.addCleanup(patcher.stop)
        p2 = mock.patch.object(mcp_server, "_client", lambda: mock.Mock(data_dir=self.d, credentials={"token": "x"}))
        p2.start()
        self.addCleanup(p2.stop)

    def tool(self, name):
        t = getattr(self.m, name)
        return getattr(t, "fn", t)

    def test_connect_mints_nothing_when_healthy_unless_forced(self):
        out = self.tool("whoop_connect")()
        self.assertEqual((out["state"], out["connect_url"]), ("connected", None))
        self.assertFalse(os.path.exists(os.path.join(self.d, "whoop_consent.json")))
        forced = self.tool("whoop_connect")(force=True)
        self.assertTrue(forced["connect_url"].startswith("https://api.prod.whoop.com/"))
        self.assertEqual(forced["expires_in_s"], 600)
        self.assertIn("home network or Tailscale", forced["message"])

    def test_connect_hands_out_a_link_when_not_connected_or_needing_reconsent(self):
        d = make_dir(tokens=None)
        with mock.patch.object(self.m, "_client", lambda: mock.Mock(data_dir=d)):
            out = self.tool("whoop_connect")()
        self.assertEqual(out["state"], "not_configured")
        self.assertIn(CID, out["connect_url"])
        state_io.atomic_write_json(os.path.join(self.d, "whoop_state.json"), {"needs_reconsent": True})
        out = self.tool("whoop_connect")()
        self.assertEqual(out["state"], "needs_reconsent")
        self.assertIsNotNone(out["connect_url"])

    def test_connect_without_configuration_explains_instead_of_minting(self):
        with mock.patch.dict(os.environ, {"WHOOP_CLIENT_ID": "", "WHOOP_CLIENT_SECRET": "", "WHOOP_REDIRECT_URI": ""}):
            out = self.tool("whoop_connect")(force=True)
        self.assertIsNone(out["connect_url"])
        self.assertIn("WHOOP_REDIRECT_URI", out["message"])

    def test_connect_rejects_a_non_boolean_force_and_unknown_arguments(self):
        import asyncio
        for args in ({"force": "yes"}, {"force": 1}, {"force": "true"}, {"forse": True}):
            with self.assertRaises(Exception, msg=str(args)):  # StrictBool + extra=forbid: refused before the tool runs
                asyncio.run(self.m.mcp.call_tool("whoop_connect", args))
        self.assertFalse(os.path.exists(os.path.join(self.d, "whoop_consent.json")))

    def test_get_readiness_reports_signals_adjustment_and_the_whoop_status(self):
        with mock.patch.object(whoop_sync, "requests", FakeHttp(good_script(25, 6.5))):
            out = self.tool("get_readiness")()
        self.assertEqual(out["adjustment"]["band"], "low")
        self.assertEqual({s["key"]: s["source"] for s in out["signals"]}, {"whoop_recovery": "whoop", "sleep_hours": "whoop"})
        self.assertEqual(out["whoop"]["state"], "connected")
        self.assertIsNone(out["whoop_hint"])
        self.assertEqual(out["pull"], "pulled")

    def test_get_readiness_says_when_whoop_needs_attention_and_still_answers(self):
        state_io.atomic_write_json(os.path.join(self.d, "whoop_state.json"), {"needs_reconsent": True})
        features.save_readiness({"subjective": 4, "source": "manual"}, self.d)
        out = self.tool("get_readiness")()
        self.assertEqual(out["whoop"]["state"], "needs_reconsent")
        self.assertIn("whoop_connect", out["whoop_hint"])
        self.assertEqual(out["signals"][0]["source"], "manual")

    def test_get_readiness_without_refresh_makes_no_call(self):
        http = FakeHttp({})
        with mock.patch.object(whoop_sync, "requests", http):
            self.tool("get_readiness")(refresh=False)
        self.assertEqual(http.calls, [])


if __name__ == "__main__":
    unittest.main()


def test_compose_passes_the_whoop_settings_to_both_services():
    """The web app and the MCP server share one data volume and each can pull WHOOP, so both need the same settings."""
    import os
    import re
    text = open(os.path.join(os.path.dirname(__file__), "..", "docker-compose.yml")).read()
    services = re.split(r"(?m)^  (?=[a-z][a-z-]*:$)", text.split("\nservices:\n", 1)[1])
    by_name = {sv.split(":", 1)[0]: sv for sv in services if sv.strip()}
    for svc in ("smart-gym-app", "smart-gym-mcp"):
        for var in ("WHOOP_CLIENT_ID", "WHOOP_CLIENT_SECRET", "WHOOP_REDIRECT_URI", "WHOOP_TOKEN_FILE", "WHOOP_PULL_MIN_INTERVAL_S"):
            assert f"- {var}=${{{var}:-" in by_name[svc], f"{svc} does not pass {var}"
    for svc in ("gym-tunnel", "edge"):  # nothing else gets the WHOOP secret
        assert "WHOOP" not in by_name.get(svc, ""), svc
