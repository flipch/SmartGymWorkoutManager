"""GYM_APP_TYPE decides who this app logs in as: NANO leaves the phone and machine sessions alone. No network."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import api_client


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body, self.text = status, body, ""
        self.content = b"x"

    def json(self):
        return self._body


class AppIdentity(unittest.TestCase):
    def _client(self):
        with mock.patch.dict(os.environ, {"DATA_DIR": tempfile.mkdtemp(prefix="gym-app-")}):
            client = api_client.SpeedianceClient()
        client.save_config("7", "tok", "Global", 1, "", 1, False, [], [], unit_confirmed=True)
        return client

    def _login_headers(self, env):
        client, seen = self._client(), []

        def request(method, url, **kw):
            seen.append((url, kw.get("headers", {})))
            if "verifyIdentity" in url:
                return Resp(200, {"code": 0, "data": {"isExist": True, "hasPwd": True}})
            return Resp(200, {"code": 0, "data": {"token": "newtoken", "appUserId": 7}})
        client._request = request
        with mock.patch.dict(os.environ, env, clear=False):
            ok, message, _ = client.login("a@example.com", "pw")
        self.assertTrue(ok, message)
        self.assertEqual(len(seen), 2)
        return seen

    def test_default_is_the_phone_app_and_nano_is_opt_in(self):
        for url, headers in self._login_headers({"GYM_APP_TYPE": ""}):
            self.assertEqual((headers["App_type"], headers["Versioncode"]), ("SOFTWARE", "40304"), url)
        for url, headers in self._login_headers({"GYM_APP_TYPE": "nano"}):
            self.assertEqual((headers["App_type"], headers["Versioncode"]), ("NANO", "1"), url)

    def test_later_requests_use_the_same_identity_as_the_login(self):
        client = self._client()
        with mock.patch.dict(os.environ, {"GYM_APP_TYPE": "NANO"}):
            headers = client._get_headers()
        self.assertEqual((headers["App_type"], headers["Versioncode"]), ("NANO", "1"))
        with mock.patch.dict(os.environ, {"GYM_APP_TYPE": ""}):
            headers = client._get_headers()
        self.assertEqual((headers["App_type"], headers["Versioncode"]), ("SOFTWARE", "40304"))

    def test_hardware_and_unknown_types_are_refused(self):
        client = self._client()
        for bad in ("HARDWARE", "BIKE", "software2"):
            with mock.patch.dict(os.environ, {"GYM_APP_TYPE": bad}):
                with self.assertRaises(RuntimeError, msg=bad):
                    client._get_headers()

    def test_a_nano_logout_forgets_the_token_locally_without_calling_the_vendor(self):
        client, calls = self._client(), []
        client._request = lambda method, url, **kw: calls.append(url) or Resp(200, {"code": 0})
        with mock.patch.dict(os.environ, {"GYM_APP_TYPE": "NANO"}):
            client.logout()
        self.assertEqual(calls, [])
        self.assertEqual(client.credentials["token"], "")
        client.save_config("7", "tok", "Global", 1, "", 1, False, [], [], unit_confirmed=True)
        with mock.patch.dict(os.environ, {"GYM_APP_TYPE": ""}):
            client.logout()
        self.assertEqual(len(calls), 1)  # the phone-app identity still tells the vendor
        self.assertEqual(client.credentials["token"], "")


if __name__ == "__main__":
    unittest.main()
