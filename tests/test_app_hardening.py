"""The REAL Flask app and MCP ASGI app (not toy apps): auth on every route, write gate, CSRF, strict dry_run."""
import base64
import os
import sys
import tempfile
import unittest
from unittest import mock

TOKEN = "t" * 32
os.environ["GYM_API_TOKEN"] = TOKEN
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="gym-test-data-")  # the app writes config/readiness/feedback here
os.environ.pop("GYM_AUTH_DISABLED", None)
os.environ.pop("GYM_ALLOW_WRITES", None)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app as gym_app  # noqa: E402  (needs the env above)
import features  # noqa: E402

BASIC = {"Authorization": "Basic " + base64.b64encode(f"gym:{TOKEN}".encode()).decode()}
BEARER = {"Authorization": f"Bearer {TOKEN}"}


class RealFlaskApp(unittest.TestCase):
    def setUp(self):
        self.c = gym_app.app.test_client()

    def test_every_route_but_healthz_requires_auth(self):
        checked = 0
        for rule in gym_app.app.url_map.iter_rules():
            if rule.rule == "/healthz":
                continue
            url = rule.rule
            for arg in rule.arguments:
                url = url.replace(f"<int:{arg}>", "1").replace(f"<string:{arg}>", "x").replace(f"<path:{arg}>", "x") \
                         .replace(f"<{arg}>", "x")
            for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
                resp = self.c.open(url, method=method)
                self.assertEqual(resp.status_code, 401, f"{method} {url}")
                checked += 1
        self.assertGreater(checked, 25)

    def test_healthz_is_open_and_reveals_nothing_about_the_vendor_session(self):
        resp = self.c.get("/healthz")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(set(resp.get_json()), {"status", "app"})

    def test_debug_endpoints_are_gone(self):
        for path in ("/debug", "/debug/last_response", "/debug/proxy", "/debug/console"):
            self.assertEqual(self.c.get(path, headers=BASIC).status_code, 404, path)

    def test_writes_are_refused_by_default_even_when_authenticated(self):
        for method, path in (("POST", "/api/schedule"), ("POST", "/api/schedule_course"), ("POST", "/delete/1"),
                             ("POST", "/logout"), ("POST", "/create"), ("POST", "/settings/unit")):
            resp = self.c.open(path, method=method, headers=BASIC, json={})
            self.assertEqual((path, resp.status_code, resp.get_json()["error"]), (path, 403, "writes_disabled"))

    def test_gate_is_default_deny_over_every_unsafe_route(self):
        """Any unsafe-method endpoint is refused without GYM_ALLOW_WRITES unless explicitly listed as local-only."""
        from auth import LOCAL_ONLY_ENDPOINTS
        endpoints = set()
        for rule in gym_app.app.url_map.iter_rules():
            if rule.methods - {"GET", "HEAD", "OPTIONS"}:
                endpoints.add(rule.endpoint)
                url = rule.rule.replace("<int:id>", "1")
                resp = self.c.post(url, headers=BASIC, json={})
                refused = resp.status_code == 403 and (resp.get_json() or {}).get("error") == "writes_disabled"
                self.assertEqual(refused, rule.endpoint not in LOCAL_ONLY_ENDPOINTS, f"{rule.endpoint} {url}")
        self.assertFalse(LOCAL_ONLY_ENDPOINTS - endpoints, "allowlist names an endpoint that no longer exists")
        self.assertIn("update_unit", endpoints)  # the route the first review caught: PUTs to the vendor account

    def test_a_route_added_later_is_gated_by_default(self):
        from flask import Flask
        from auth import install_flask_write_gate
        fresh = Flask("fresh")
        install_flask_write_gate(fresh)

        @fresh.route("/brand-new", methods=["POST"])
        def brand_new():
            return "changed vendor state"

        resp = fresh.test_client().post("/brand-new")
        self.assertEqual((resp.status_code, resp.get_json()["error"]), (403, "writes_disabled"))

    def test_unit_change_is_refused_without_writes(self):
        with mock.patch.object(gym_app.client, "update_unit") as upd:
            resp = self.c.post("/settings/unit", headers=BASIC, data={"unit": "1"})
        self.assertEqual(resp.status_code, 403)
        upd.assert_not_called()

    def test_proxy_that_rewrites_host_works_with_forwarded_host_or_allowlist(self):
        browser = {**BASIC, "Origin": "https://gym.example.com", "Sec-Fetch-Site": "same-origin"}
        with mock.patch.dict(os.environ, {"GYM_ALLOW_WRITES": "1"}), mock.patch.object(gym_app.client, "delete_workout"):
            self.assertEqual(self.c.post("/delete/1", headers=browser).status_code, 403)  # Host seen is localhost
            self.assertEqual(self.c.post("/delete/1", headers={**browser, "X-Forwarded-Host": "gym.example.com"}).status_code, 302)
            with mock.patch.dict(os.environ, {"GYM_ALLOWED_ORIGINS": "other.example, gym.example.com"}):
                self.assertEqual(self.c.post("/delete/1", headers=browser).status_code, 302)
            self.assertEqual(self.c.post("/delete/1", headers={**browser, "Origin": "https://evil.example",
                                                               "X-Forwarded-Host": "gym.example.com"}).status_code, 403)

    def test_pages_with_the_new_post_forms_actually_render(self):
        """The Delete / Logout links became POST forms; render the real templates with a stubbed signed-in client."""
        creds = {"token": "x", "user_id": "42", "unit": 0, "region": "Global", "device_type": 1,
                 "owned_accessories": [], "owned_devices": [], "custom_instruction": "", "allow_monster_moves": False}
        workouts = [{"id": 5, "code": "abc", "title": "Push", "name": "Push", "totalCapacity": 100, "durationMinute": 30,
                     "actionNum": 4}]
        with mock.patch.object(gym_app.client, "credentials", creds), \
                mock.patch.object(gym_app.client, "load_config", return_value=creds), \
                mock.patch.object(gym_app.client, "get_user_workouts", return_value=workouts), \
                mock.patch.object(gym_app.client, "get_accessories", return_value=[]):
            home = self.c.get("/", headers=BASIC)
            settings = self.c.get("/settings", headers=BASIC)
        self.assertEqual((home.status_code, settings.status_code), (200, 200))
        self.assertIn(b'<form method="post" action="/delete/5"', home.data)
        self.assertNotIn(b'href="/delete/', home.data)
        self.assertIn(b'<form method="post" action="/logout"', settings.data)
        self.assertNotIn(b'href="/logout"', settings.data)

    def test_host_comparison_ignores_case(self):
        from auth import allowed_origins, cross_site
        hosts = allowed_origins("localhost", "Gym.Lan.Doghous.Cloud", "OTHER.example")
        for origin in ("https://gym.lan.doghous.cloud", "https://Gym.Lan.Doghous.Cloud", "http://other.example"):
            self.assertFalse(cross_site({"Origin": origin}, hosts), origin)
        self.assertTrue(cross_site({"Origin": "https://gym.lan.doghous.cloud.evil.com"}, hosts))

    def test_malformed_forwarded_host_cannot_whitelist_a_null_origin(self):
        from auth import allowed_origins, cross_site
        for xfh in (", x", ",", " ", ""):
            hosts = allowed_origins("localhost", xfh, " , ,")
            self.assertNotIn("", hosts, repr(xfh))
            self.assertTrue(cross_site({"Origin": "null"}, hosts), repr(xfh))

    def test_delete_and_logout_are_no_longer_get_requests(self):
        self.assertEqual(self.c.get("/delete/1", headers=BASIC).status_code, 405)
        self.assertEqual(self.c.get("/logout", headers=BASIC).status_code, 405)

    def test_cross_site_unsafe_requests_are_refused_even_with_credentials(self):
        with mock.patch.dict(os.environ, {"GYM_ALLOW_WRITES": "1"}):
            for extra in ({"Origin": "https://evil.example"}, {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"}):
                resp = self.c.post("/delete/1", headers={**BASIC, **extra})
                self.assertEqual((extra, resp.status_code), (extra, 403))
                self.assertEqual(resp.get_json()["error"], "cross_site_request_refused")

    def test_same_origin_and_non_browser_requests_pass_the_gate(self):
        with mock.patch.dict(os.environ, {"GYM_ALLOW_WRITES": "1"}), mock.patch.object(gym_app.client, "delete_workout"):
            ok = self.c.post("/delete/1", headers={**BASIC, "Origin": "http://localhost", "Sec-Fetch-Site": "same-origin"})
            self.assertEqual(ok.status_code, 302)
            self.assertEqual(self.c.post("/delete/1", headers=BASIC).status_code, 302)  # curl: no Origin header

    def test_coach_apply_only_a_literal_false_is_a_real_save(self):
        body = {"program": {"days": [{"name": "d", "exercises": []}]}}
        seen = []
        with mock.patch.object(features, "_authed", return_value=True), \
                mock.patch.object(features, "apply_program", side_effect=lambda *a: seen.append(a[4]) or {"ok": True}):
            for value in (None, 0, "", "false", True, "no"):
                self.c.post("/api/coach/apply", headers=BASIC, json={**body, "dry_run": value})
            self.c.post("/api/coach/apply", headers=BASIC, json=body)  # omitted
            self.assertEqual(seen, [True] * 7)
            refused = self.c.post("/api/coach/apply", headers=BASIC, json={**body, "dry_run": False})
            self.assertEqual((refused.status_code, refused.get_json()["error"]), (403, "writes_disabled"))
            self.assertEqual(len(seen), 7, "a refused real save must not reach apply_program")
            with mock.patch.dict(os.environ, {"GYM_ALLOW_WRITES": "1"}):
                self.c.post("/api/coach/apply", headers=BASIC, json={**body, "dry_run": False})
            self.assertEqual(seen[-1], False)


class RealMcpApp(unittest.TestCase):
    def test_build_app_rejects_missing_and_wrong_tokens_and_has_the_wrapper(self):
        import mcp_server
        from auth import BearerAuthASGI
        app = mcp_server.build_app("127.0.0.1")
        self.assertIsInstance(app, BearerAuthASGI)
        from starlette.testclient import TestClient
        client = TestClient(app, raise_server_exceptions=False)
        self.assertEqual(client.post("/mcp", json={}).status_code, 401)
        self.assertEqual(client.post("/mcp", json={}, headers={"Authorization": "Bearer wrong"}).status_code, 401)
        self.assertNotEqual(client.post("/mcp", json={}, headers=BEARER).status_code, 401)


if __name__ == "__main__":
    unittest.main()
