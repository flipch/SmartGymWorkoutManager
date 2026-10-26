"""Speediance read-only adapter: session handling, failure semantics, pacing. No network."""
import json
import os
import stat
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from harness.errors import SchemaDrift, SourceAuthError, SourceDown
from harness.speediance import SpeedianceNano

PASSWORD = "hunter2-do-not-leak"


class FakeApi:
    """Scripted transport. `script` maps a path prefix to a list of (status, payload) answers, consumed in order."""

    def __init__(self, script):
        self.script, self.calls = script, []

    def __call__(self, method, url, headers, body):
        path = url.split("speediance.com", 1)[1]
        self.calls.append((method, path, dict(headers), body))
        for prefix, answers in self.script.items():
            if path.startswith(prefix):
                item = answers.pop(0) if len(answers) > 1 else answers[0]
                if isinstance(item, Exception):
                    raise item
                return item
        raise AssertionError(f"unscripted call {path}")


OK_LOGIN = {
    "/api/app/v2/login/verifyIdentity": [(200, {"code": 0, "data": {"isExist": True, "hasPwd": True}})],
    "/api/app/v2/login/byPass": [(200, {"code": 0, "data": {"token": "tok-1", "appUserId": 42}})],
}


class AdapterCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.creds = self.dir / "creds.env"
        self.creds.write_text(f"SPEEDIANCE_EMAIL=me@example.com\nSPEEDIANCE_PASSWORD={PASSWORD}\n")
        os.chmod(self.creds, 0o600)
        self.token = self.dir / "nano.token.json"
        self.sleeps = []
        self.now = [1_000_000.0]

    def make(self, script, **kw):
        api = FakeApi(script)

        def sleep(seconds):
            self.sleeps.append(seconds)
            self.now[0] += seconds

        adapter = SpeedianceNano(self.creds, self.token, transport=api, clock=lambda: self.now[0], sleep=sleep, **kw)
        return adapter, api


class Login(AdapterCase):
    def test_logs_in_as_nano_with_versioncode_1_and_caches_token_privately(self):
        script = {**OK_LOGIN, "/api/mobile/v2/report": [(200, {"code": 0, "data": []})]}
        adapter, api = self.make(script)
        adapter.history(date(2026, 10, 1), date(2026, 10, 26))
        login_calls = [c for c in api.calls if "login" in c[1]]
        self.assertEqual(len(login_calls), 2)
        for _, _, headers, _ in login_calls:
            self.assertEqual(headers["App_type"], "NANO")
            self.assertEqual(headers["Versioncode"], "1")
            self.assertNotIn("Token", headers)
        history_headers = api.calls[-1][2]
        self.assertEqual(history_headers["Token"], "tok-1")
        self.assertEqual(history_headers["App_user_id"], "42")
        self.assertEqual(stat.S_IMODE(self.token.stat().st_mode), 0o600)
        self.assertEqual(adapter.logins, 1)

    def test_second_instance_reuses_cached_token_without_login(self):
        script = {**OK_LOGIN, "/api/mobile/v2/report": [(200, {"code": 0, "data": []})]}
        first, _ = self.make(script)
        first.history(date(2026, 10, 1), date(2026, 10, 2))
        second, api2 = self.make({"/api/mobile/v2/report": [(200, {"code": 0, "data": []})]})
        second.history(date(2026, 10, 1), date(2026, 10, 2))
        self.assertEqual(second.logins, 0)
        self.assertFalse(any("login" in c[1] for c in api2.calls))

    def test_there_is_no_logout(self):
        adapter, _ = self.make(OK_LOGIN)
        self.assertFalse(hasattr(adapter, "logout"))

    def test_every_request_is_a_get_or_one_of_the_two_login_posts(self):
        """Behavioural guard for 'never log the user out / never write': drive every public path incl. re-login."""
        script = {**OK_LOGIN,
                  "/api/mobile/v2/report": [(200, {"code": 90, "message": "session gone"}), (200, {"code": 0, "data": []})],
                  "/api/app/trainingInfo": [(200, {"code": 0, "data": []})]}
        adapter, api = self.make(script)
        adapter.history(date(2026, 10, 1), date(2026, 10, 2))   # includes forced re-login on code 90
        for record_type in (2, 5):
            adapter.detail(1, record_type)
        self.assertGreater(len(api.calls), 4)
        allowed_posts = {"/api/app/v2/login/verifyIdentity", "/api/app/v2/login/byPass"}
        for method, path, _, _ in api.calls:
            self.assertTrue(method == "GET" or (method == "POST" and path in allowed_posts), f"{method} {path}")
            self.assertNotRegex(path.lower(), r"logout|save|delete|schedule|push|send|create|update")

    def test_no_harness_module_mentions_a_write_or_logout_endpoint(self):
        import re
        for src in (Path(__file__).parent.parent / "harness").glob("*.py"):
            for n, line in enumerate(src.read_text().splitlines(), 1):
                if line.lstrip().startswith(("#", '"""')):
                    continue
                self.assertNotRegex(line.lower(), r"[\"'/](logout|saveworkout|deleteworkout|schedule)\b", f"{src.name}:{n}")

    def test_refuses_world_readable_credentials(self):
        os.chmod(self.creds, 0o644)
        adapter, _ = self.make(OK_LOGIN)
        with self.assertRaises(PermissionError):
            adapter.history(date(2026, 10, 1), date(2026, 10, 2))

    def test_wrong_password_raises_without_leaking_it(self):
        script = {**OK_LOGIN, "/api/app/v2/login/byPass": [(200, {"code": 1001, "message": "bad credentials"})]}
        adapter, _ = self.make(script)
        with self.assertRaises(SourceDown) as ctx:
            adapter.history(date(2026, 10, 1), date(2026, 10, 2))
        self.assertNotIn(PASSWORD, str(ctx.exception))
        self.assertFalse(self.token.exists())


class Failures(AdapterCase):
    def test_session_gone_relogs_in_once_and_retries_once(self):
        script = {
            **OK_LOGIN,
            "/api/mobile/v2/report": [(200, {"code": 91, "message": "expired"}), (200, {"code": 0, "data": [{"trainingId": 1}]})],
        }
        adapter, api = self.make(script)
        self.assertEqual(adapter.history(date(2026, 10, 1), date(2026, 10, 2)), [{"trainingId": 1}])
        self.assertEqual(adapter.logins, 2)  # initial + exactly one re-login

    def test_second_rejection_raises_instead_of_looping(self):
        script = {**OK_LOGIN, "/api/mobile/v2/report": [(200, {"code": 90, "message": "offline"})]}
        adapter, _ = self.make(script)
        with self.assertRaises(SourceAuthError):
            adapter.history(date(2026, 10, 1), date(2026, 10, 2))
        self.assertEqual(adapter.logins, 2)

    def test_api_error_raises_instead_of_empty_list(self):
        script = {**OK_LOGIN, "/api/mobile/v2/report": [(200, {"code": 500, "message": "boom"})]}
        adapter, _ = self.make(script)
        with self.assertRaises(SourceDown):
            adapter.history(date(2026, 10, 1), date(2026, 10, 2))

    def test_http_error_raises(self):
        script = {**OK_LOGIN, "/api/mobile/v2/report": [(503, {"_non_json": "<html>"})]}
        adapter, _ = self.make(script)
        with self.assertRaises(SourceDown):
            adapter.history(date(2026, 10, 1), date(2026, 10, 2))

    def test_network_error_raises_source_down(self):
        script = {**OK_LOGIN, "/api/mobile/v2/report": [TimeoutError("slow")]}
        adapter, _ = self.make(script)
        with self.assertRaises(SourceDown) as ctx:
            adapter.history(date(2026, 10, 1), date(2026, 10, 2))
        self.assertIn("TimeoutError", str(ctx.exception))

    def test_schema_drift_when_data_is_not_a_list(self):
        script = {**OK_LOGIN, "/api/mobile/v2/report": [(200, {"code": 0, "data": {"oops": 1}})]}
        adapter, _ = self.make(script)
        with self.assertRaises(SchemaDrift):
            adapter.history(date(2026, 10, 1), date(2026, 10, 2))

    def test_genuinely_empty_history_is_an_empty_list(self):
        script = {**OK_LOGIN, "/api/mobile/v2/report": [(200, {"code": 0, "data": []})]}
        adapter, _ = self.make(script)
        self.assertEqual(adapter.history(date(2026, 10, 1), date(2026, 10, 2)), [])


class DetailAndPacing(AdapterCase):
    def test_detail_picks_endpoint_by_record_type_and_rejects_types_without_detail(self):
        script = {**OK_LOGIN, "/api/app/trainingInfo": [(200, {"code": 0, "data": []})]}
        adapter, api = self.make(script)
        adapter.detail(123, 5)
        adapter.detail(456, 2)
        paths = [c[1] for c in api.calls if "trainingInfo" in c[1]]
        self.assertEqual(paths, ["/api/app/trainingInfo/cttTrainingInfoDetail/123",
                                 "/api/app/trainingInfo/courseTrainingInfoDetail/456"])
        with self.assertRaises(SourceDown):
            adapter.detail(789, 1)

    def test_requests_are_paced(self):
        script = {**OK_LOGIN, "/api/app/trainingInfo": [(200, {"code": 0, "data": []})]}
        adapter, _ = self.make(script, min_gap_s=1.5)
        adapter.detail(1, 5)
        adapter.detail(2, 5)
        self.assertTrue(self.sleeps and all(s >= 0 for s in self.sleeps))
        self.assertGreaterEqual(sum(self.sleeps), 1.5 * 3 - 0.01)  # 4 calls -> at least 3 gaps


if __name__ == "__main__":
    unittest.main()
