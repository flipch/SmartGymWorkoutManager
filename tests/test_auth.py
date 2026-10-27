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


class CloudflareAccessAssertion(unittest.TestCase):
    """The origin trusts Cf-Access-Jwt-Assertion only when it verifies; the header is forgeable by anyone on the LAN."""
    TEAM, AUD, EMAIL = "doghous", "a" * 64, "me@example.com"

    @classmethod
    def setUpClass(cls):
        from cryptography.hazmat.primitives.asymmetric import rsa
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.other = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def setUp(self):
        from starlette.applications import Starlette
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route
        from starlette.testclient import TestClient

        async def inner_mcp(request):
            return PlainTextResponse("in")

        self.access = auth.AccessJwt(self.TEAM, self.AUD, [self.EMAIL], key_for=lambda token: self.key.public_key())
        self.c = TestClient(auth.BearerAuthASGI(Starlette(routes=[Route("/mcp", inner_mcp, methods=["POST"])]), TOKEN, self.access))

    def jwt(self, key=None, alg="RS256", **override):
        import time
        import jwt as pyjwt
        now = int(time.time())
        claims = {"iss": f"https://{self.TEAM}.cloudflareaccess.com", "aud": [self.AUD], "email": self.EMAIL,
                  "iat": now, "exp": now + 300}
        claims.update(override)
        claims = {k: v for k, v in claims.items() if v is not None}
        return pyjwt.encode(claims, key or self.key, algorithm=alg)

    def post(self, token):
        return self.c.post("/mcp", headers={"Cf-Access-Jwt-Assertion": token}).status_code

    def test_a_valid_assertion_for_an_allowed_email_passes(self):
        self.assertEqual(self.post(self.jwt()), 200)
        self.assertEqual(self.post(self.jwt(email=self.EMAIL.upper())), 200)

    def test_every_way_an_assertion_can_be_wrong_is_refused(self):
        bad = {
            "another key": self.jwt(key=self.other),
            "wrong audience": self.jwt(aud=["b" * 64]),
            "wrong issuer": self.jwt(iss="https://evil.cloudflareaccess.com"),
            "expired": self.jwt(exp=1_600_000_000, iat=1_599_999_000),
            "no expiry": self.jwt(exp=None),
            "other email": self.jwt(email="mallory@example.com"),
            "no email (a service token)": self.jwt(email=None, common_name="abc.access"),
            "email not a string": self.jwt(email=["me@example.com"]),
            "HS256 signed with the public key bytes": self.jwt(key="x" * 64, alg="HS256"),
            "garbage": "not.a.jwt",
            "empty": "",
        }
        for name, token in bad.items():
            self.assertEqual(self.post(token), 401, name)

    def test_alg_none_is_refused(self):
        import base64, json
        b = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
        import time
        forged = b({"alg": "none", "typ": "JWT"}) + "." + b({"iss": self.access.issuer, "aud": [self.AUD], "email": self.EMAIL,
                                                             "iat": int(time.time()), "exp": int(time.time()) + 300}) + "."
        self.assertEqual(self.post(forged), 401)

    def test_a_key_lookup_failure_is_refused_not_trusted(self):
        def boom(token):
            raise OSError("certs endpoint unreachable")
        access = auth.AccessJwt(self.TEAM, self.AUD, [self.EMAIL], key_for=boom)
        from starlette.applications import Starlette
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route
        from starlette.testclient import TestClient

        async def inner(request):
            return PlainTextResponse("in")
        c = TestClient(auth.BearerAuthASGI(Starlette(routes=[Route("/mcp", inner, methods=["POST"])]), TOKEN, access))
        self.assertEqual(c.post("/mcp", headers={"Cf-Access-Jwt-Assertion": self.jwt()}).status_code, 401)

    def test_the_shared_secret_still_works_and_a_bad_assertion_does_not_block_it(self):
        r = self.c.post("/mcp", headers={"Authorization": f"Bearer {TOKEN}", "Cf-Access-Jwt-Assertion": "garbage"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.c.post("/mcp").status_code, 401)

    def test_without_access_configuration_the_header_is_ignored(self):
        from starlette.applications import Starlette
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route
        from starlette.testclient import TestClient

        async def inner(request):
            return PlainTextResponse("in")
        c = TestClient(auth.BearerAuthASGI(Starlette(routes=[Route("/mcp", inner, methods=["POST"])]), TOKEN))
        self.assertEqual(c.post("/mcp", headers={"Cf-Access-Jwt-Assertion": self.jwt()}).status_code, 401)

    def test_configuration_is_all_or_nothing(self):
        env = {"GYM_ACCESS_TEAM": "", "GYM_ACCESS_AUD": "", "GYM_ACCESS_EMAILS": ""}
        with patch.dict(os.environ, env):
            self.assertIsNone(auth.load_access())
        for partial in ({"GYM_ACCESS_TEAM": "doghous"}, {"GYM_ACCESS_TEAM": "doghous", "GYM_ACCESS_AUD": self.AUD},
                        {"GYM_ACCESS_TEAM": "Bad Team!", "GYM_ACCESS_AUD": self.AUD, "GYM_ACCESS_EMAILS": self.EMAIL},
                        {"GYM_ACCESS_TEAM": "evil.com/x", "GYM_ACCESS_AUD": self.AUD, "GYM_ACCESS_EMAILS": self.EMAIL},
                        {"GYM_ACCESS_TEAM": "doghous", "GYM_ACCESS_AUD": "short", "GYM_ACCESS_EMAILS": self.EMAIL}):
            with patch.dict(os.environ, {**env, **partial}):
                with self.assertRaises(RuntimeError, msg=str(partial)):
                    auth.load_access()
        with patch.dict(os.environ, {"GYM_ACCESS_TEAM": "doghous", "GYM_ACCESS_AUD": self.AUD, "GYM_ACCESS_EMAILS": f" {self.EMAIL} , "}):
            self.assertEqual(auth.load_access().emails, {self.EMAIL})

    def test_a_token_with_the_wrong_claims_costs_no_key_fetch_and_a_failed_fetch_cools_down(self):
        calls = []

        def counting(token):
            calls.append(token)
            return self.key.public_key()
        access = auth.AccessJwt(self.TEAM, self.AUD, [self.EMAIL], key_for=counting)
        for bad in (self.jwt(email="mallory@example.com"), self.jwt(aud=["b" * 64]), self.jwt(iss="https://evil.cloudflareaccess.com"),
                    self.jwt(exp=1_600_000_000, iat=1_599_999_000), "garbage", ""):
            self.assertIsNone(access.email(bad))
        self.assertEqual(calls, [], "claims that cannot pass must not reach the key lookup")
        self.assertEqual(access.email(self.jwt()), self.EMAIL)
        self.assertEqual(len(calls), 1)
        # the real key lookup: an unreachable endpoint with nothing cached fetches once, then refuses until the cooldown ends
        import jwt as pyjwt
        fetches = []

        def down(self_, *a, **k):
            fetches.append(1)
            raise pyjwt.PyJWKClientConnectionError("down")
        with patch.object(pyjwt.PyJWKClient, "fetch_data", down):
            real = auth.AccessJwt(self.TEAM, self.AUD, [self.EMAIL])
            for _ in range(5):
                self.assertIsNone(real.email(self.jwt()))
        self.assertEqual(len(fetches), 1)

    def test_a_forged_unknown_kid_cannot_lock_the_real_user_out(self):
        import json as _json
        import jwt as pyjwt
        from jwt.algorithms import RSAAlgorithm
        jwk = _json.loads(RSAAlgorithm.to_jwk(self.key.public_key()))
        jwk.update({"kid": "real", "use": "sig", "alg": "RS256"})
        with patch.object(pyjwt.PyJWKClient, "fetch_data", lambda self_: {"keys": [jwk]}):
            real = auth.AccessJwt(self.TEAM, self.AUD, [self.EMAIL])
            good = lambda: pyjwt.encode(self.claims(), self.key, algorithm="RS256", headers={"kid": "real"})
            forged = pyjwt.encode(self.claims(), self.other, algorithm="RS256", headers={"kid": "unknown"})
            self.assertEqual(real.email(good()), self.EMAIL)
            self.assertIsNone(real.email(forged))  # passes the pre-filter, misses the kid
            self.assertEqual(real.email(good()), self.EMAIL, "one forged request must not lock the real user out")
            for _ in range(10):
                self.assertIsNone(real.email(forged))
                self.assertEqual(real.email(good()), self.EMAIL)

    def test_a_failing_key_endpoint_is_fetched_once_per_cooldown_and_cached_keys_keep_working(self):
        import json as _json
        import jwt as pyjwt
        from jwt.algorithms import RSAAlgorithm
        jwk = _json.loads(RSAAlgorithm.to_jwk(self.key.public_key()))
        jwk.update({"kid": "real", "use": "sig", "alg": "RS256"})
        state = {"up": True, "fetches": 0}

        def fetch(self_):
            state["fetches"] += 1
            if not state["up"]:
                raise pyjwt.PyJWKClientConnectionError("down")
            return {"keys": [jwk]}
        good = lambda: pyjwt.encode(self.claims(), self.key, algorithm="RS256", headers={"kid": "real"})
        with patch.object(pyjwt.PyJWKClient, "fetch_data", fetch):
            real = auth.AccessJwt(self.TEAM, self.AUD, [self.EMAIL])
            self.assertEqual(real.email(good()), self.EMAIL)
            state["up"], state["fetches"] = False, 0
            for i in range(30):  # a flood of plausible forgeries naming unknown kids while the endpoint is down
                forged = pyjwt.encode(self.claims(), self.other, algorithm="RS256", headers={"kid": f"unknown-{i}"})
                self.assertIsNone(real.email(forged))
                self.assertEqual(real.email(good()), self.EMAIL, "a cached key must keep verifying while the endpoint is down")
        self.assertLessEqual(state["fetches"], 2, "the flood must not become one network fetch per request")

    def claims(self):
        import time
        now = int(time.time())
        return {"iss": f"https://{self.TEAM}.cloudflareaccess.com", "aud": [self.AUD], "email": self.EMAIL, "iat": now, "exp": now + 300}


class TunnelIsolation(unittest.TestCase):
    """The tunnel shares a network with the MCP service only: a wrong dashboard route cannot reach the web UI."""

    def blocks(self):
        import re
        text = open(os.path.join(os.path.dirname(__file__), "..", "docker-compose.yml")).read()
        services = text.split("\nservices:\n", 1)[1].split("\nnetworks:\n", 1)[0]
        return {m.group(1): m.group(2) for m in re.finditer(r"^  ([a-z-]+):\n((?:    .*\n|\n)+)", services + "\n", re.M)}

    def test_only_the_mcp_and_the_tunnel_are_on_the_edge_network(self):
        blocks = self.blocks()
        self.assertEqual(set(blocks), {"smart-gym-app", "smart-gym-mcp", "gym-tunnel"})
        self.assertIn("networks: [default, edge]", blocks["smart-gym-mcp"])
        self.assertIn("networks: [edge]", blocks["gym-tunnel"])
        self.assertNotIn("edge", blocks["smart-gym-app"])
        self.assertIn("profiles: [chatgpt]", blocks["gym-tunnel"])  # opt-in, never started by a plain `up`
