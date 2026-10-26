"""Auth, dry-run default and write-gate tests. No network, no real Speediance calls."""
import base64
import inspect
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import auth

TOKEN = "t" * 32


def _basic(pw, user="x"):
    return "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()


class AuthorizedHeader(unittest.TestCase):
    def test_bearer_ok(self):
        self.assertTrue(auth.authorized(f"Bearer {TOKEN}", TOKEN))

    def test_basic_ok_any_username(self):
        self.assertTrue(auth.authorized(_basic(TOKEN, "anyone"), TOKEN))

    def test_rejects_missing_wrong_malformed(self):
        for bad in ("", "Bearer", "Bearer wrong", _basic("wrong"), "Basic !!!notbase64", "Token " + TOKEN):
            self.assertFalse(auth.authorized(bad, TOKEN), bad)


class LoadToken(unittest.TestCase):
    def test_fails_closed_without_token(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError):
                auth.load_token()

    def test_rejects_short_token(self):
        with patch.dict(os.environ, {"GYM_API_TOKEN": "short"}, clear=True):
            with self.assertRaises(RuntimeError):
                auth.load_token()

    def test_explicit_disable(self):
        with patch.dict(os.environ, {"GYM_AUTH_DISABLED": "1"}, clear=True):
            self.assertIsNone(auth.load_token())

    def test_returns_token(self):
        with patch.dict(os.environ, {"GYM_API_TOKEN": TOKEN}, clear=True):
            self.assertEqual(auth.load_token(), TOKEN)


class FlaskGate(unittest.TestCase):
    def setUp(self):
        from flask import Flask
        app = Flask(__name__)

        @app.route("/healthz")
        def healthz():
            return "ok"

        @app.route("/secret", methods=["GET", "POST"])
        def secret():
            return "data"

        auth.install_flask_auth(app, TOKEN)
        self.c = app.test_client()

    def test_healthz_exempt(self):
        self.assertEqual(self.c.get("/healthz").status_code, 200)

    def test_unauthenticated_gets_401_with_challenge(self):
        r = self.c.get("/secret")
        self.assertEqual(r.status_code, 401)
        self.assertIn("Basic", r.headers["WWW-Authenticate"])
        self.assertEqual(self.c.post("/secret").status_code, 401)

    def test_authenticated_ok(self):
        self.assertEqual(self.c.get("/secret", headers={"Authorization": f"Bearer {TOKEN}"}).status_code, 200)
        self.assertEqual(self.c.get("/secret", headers={"Authorization": _basic(TOKEN)}).status_code, 200)
        self.assertEqual(self.c.get("/secret", headers={"Authorization": _basic("nope")}).status_code, 401)


class AsgiGate(unittest.TestCase):
    def setUp(self):
        from starlette.applications import Starlette
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route
        from starlette.testclient import TestClient

        async def inner_mcp(request):
            return PlainTextResponse("in")

        async def inner_health(request):
            return PlainTextResponse("ok")

        inner = Starlette(routes=[Route("/mcp", inner_mcp, methods=["GET", "POST"]),
                                  Route("/healthz", inner_health)])
        self.c = TestClient(auth.BearerAuthASGI(inner, TOKEN))

    def test_blocks_without_token(self):
        r = self.c.post("/mcp", json={})
        self.assertEqual(r.status_code, 401)
        self.assertIn("Bearer", r.headers["www-authenticate"])

    def test_blocks_wrong_token(self):
        self.assertEqual(self.c.post("/mcp", headers={"Authorization": "Bearer nope"}).status_code, 401)

    def test_allows_token_and_healthz(self):
        self.assertEqual(self.c.post("/mcp", headers={"Authorization": f"Bearer {TOKEN}"}).text, "in")
        self.assertEqual(self.c.get("/healthz").status_code, 200)


class WriteSafetyDefaults(unittest.TestCase):
    def test_dry_run_defaults_true_everywhere(self):
        import features
        import mcp_server
        self.assertTrue(inspect.signature(features.apply_program).parameters["dry_run"].default)
        fn = getattr(mcp_server.create_workout_from_program, "fn", mcp_server.create_workout_from_program)
        self.assertTrue(inspect.signature(fn).parameters["dry_run"].default)

    def test_write_tools_refuse_when_writes_disabled(self):
        import mcp_server

        def raw(tool):
            return getattr(tool, "fn", tool)

        with patch.dict(os.environ, {}, clear=True):
            for call in (lambda: raw(mcp_server.delete_workout)("abc"),
                         lambda: raw(mcp_server.save_workout)("w", []),
                         lambda: raw(mcp_server.schedule_workout)("2026-10-27", "code"),
                         lambda: raw(mcp_server.create_workout_from_program)({"days": [1]}, dry_run=False)):
                self.assertEqual(call()["error"], "writes_disabled")


if __name__ == "__main__":
    unittest.main()
