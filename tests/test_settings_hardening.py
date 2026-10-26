"""Settings page, session secret, credential file mode, dev-server options (review round 12)."""
import base64
import json
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

TOKEN = "t" * 32
os.environ["GYM_API_TOKEN"] = TOKEN
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="gym-test-data-")
os.environ.pop("GYM_AUTH_DISABLED", None)
os.environ.pop("GYM_ALLOW_WRITES", None)
os.environ.pop("GYM_SECRET_KEY", None)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import api_client  # noqa: E402
import app as gym_app  # noqa: E402

BASIC = {"Authorization": "Basic " + base64.b64encode(f"gym:{TOKEN}".encode()).decode()}
VENDOR_TOKEN = "vendor-session-token-ABC123"


def make_client(data_dir, token=VENDOR_TOKEN):
    with mock.patch.dict(os.environ, {"DATA_DIR": data_dir}):
        client = api_client.SpeedianceClient()
    if token:
        client.save_config("42", token, "Global", 0, "", 1, False, [], [])
    client.get_accessories = lambda: []
    return client


class SessionSecret(unittest.TestCase):
    def test_not_the_published_constant_and_long_enough(self):
        self.assertNotEqual(gym_app.app.secret_key, "speediance_secret_key")
        self.assertGreaterEqual(len(gym_app.app.secret_key), 32)

    @staticmethod
    def _secret_in_fresh_process(**extra):
        import subprocess
        repo = os.path.join(os.path.dirname(__file__), "..")
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "DATA_DIR": tempfile.mkdtemp(), **extra}
        env.pop("GYM_SECRET_KEY", None) if "GYM_SECRET_KEY" not in extra else None
        out = subprocess.run([sys.executable, "-B", "-c", "import app; print(app.app.secret_key)"], cwd=repo,
                             env=env, capture_output=True, text=True, check=True)
        return out.stdout.strip().splitlines()[-1]

    def test_env_override_is_used(self):
        self.assertEqual(self._secret_in_fresh_process(GYM_SECRET_KEY="k" * 40), "k" * 40)

    def test_default_differs_between_processes(self):
        self.assertNotEqual(self._secret_in_fresh_process(), self._secret_in_fresh_process())


class CredentialFile(unittest.TestCase):
    def test_config_json_is_0600_and_leaves_no_temp_files(self):
        d = tempfile.mkdtemp()
        make_client(d)
        path = os.path.join(d, "config.json")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(sorted(os.listdir(d)), ["config.json"])
        self.assertEqual(json.load(open(path))["token"], VENDOR_TOKEN)

    def test_existing_loose_file_is_tightened_on_next_save(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "config.json")
        with open(path, "w") as fh:
            json.dump({"token": "old"}, fh)
        os.chmod(path, 0o644)
        make_client(d)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)


class SettingsPage(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.client = make_client(self.d)
        p = mock.patch.object(gym_app, "client", self.client)
        p.start()
        self.addCleanup(p.stop)
        self.c = gym_app.app.test_client()

    def test_page_never_contains_the_vendor_token(self):
        body = self.c.get("/settings", headers=BASIC).get_data(as_text=True)
        self.assertNotIn(VENDOR_TOKEN, body)
        self.assertIn("leave blank to keep", body)
        self.assertIn('type="password" name="token" value=""', body)

    def test_signed_out_page_asks_for_a_token(self):
        gym_app.client = make_client(tempfile.mkdtemp(), token=None)
        body = self.c.get("/settings", headers=BASIC).get_data(as_text=True)
        self.assertIn("Paste a token", body)

    def test_blank_token_keeps_the_stored_one(self):
        resp = self.c.post("/settings", headers=BASIC, data={"user_id": "", "token": "", "region": "EU"})
        self.assertEqual(resp.status_code, 302)
        saved = json.load(open(os.path.join(self.d, "config.json")))
        self.assertEqual((saved["token"], saved["user_id"], saved["region"]), (VENDOR_TOKEN, "42", "EU"))

    def test_a_new_token_replaces_it(self):
        self.c.post("/settings", headers=BASIC, data={"user_id": "9", "token": "fresh", "region": "Global"})
        saved = json.load(open(os.path.join(self.d, "config.json")))
        self.assertEqual((saved["token"], saved["user_id"]), ("fresh", "9"))

    def test_failed_login_flashes_the_message_but_not_the_debug_blob(self):
        self.client.login = lambda e, p: (False, "Login failed", "REQ password=hunter2 resp token=zzz")
        resp = self.c.post("/login", headers=BASIC, data={"email": "a@b.c", "password": "hunter2"},
                           follow_redirects=True)
        body = resp.get_data(as_text=True)
        self.assertIn("Login failed", body)
        self.assertNotIn("hunter2", body)
        self.assertNotIn("token=zzz", body)


class DevServer(unittest.TestCase):
    def test_default_is_loopback_and_not_debug(self):
        self.assertEqual(gym_app.dev_server_options({}), {"debug": False, "host": "127.0.0.1", "port": 5001})

    def test_debug_only_when_explicitly_one(self):
        for value, want in (("1", True), ("0", False), ("true", False), ("", False)):
            self.assertEqual(gym_app.dev_server_options({"GYM_DEBUG": value})["debug"], want, value)

    def test_bind_override(self):
        self.assertEqual(gym_app.dev_server_options({"GYM_BIND": "0.0.0.0"})["host"], "0.0.0.0")

    def test_the_main_block_uses_it(self):
        src = open(os.path.join(os.path.dirname(__file__), "..", "app.py")).read()
        self.assertIn("app.run(**dev_server_options())", src)
        self.assertNotIn("debug=True", src)


if __name__ == "__main__":
    unittest.main()
