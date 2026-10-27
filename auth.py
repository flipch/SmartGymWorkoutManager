"""
Shared-secret authentication for the Smart Gym web UI (Flask) and MCP server (ASGI).

Both services read the same secret from the environment (GYM_API_TOKEN) and fail closed:
if it is unset they refuse to start unless GYM_AUTH_DISABLED=1 is set explicitly.

Accepted credentials:
  * Authorization: Bearer <token>            (agents / MCP clients)
  * Authorization: Basic base64(any:<token>) (browsers; the username is ignored)

/healthz is exempt so Docker and Uptime Kuma keep working.
"""
import asyncio
import base64
import hmac
import os
import re

EXEMPT_PATHS = frozenset({"/healthz"})


def load_token() -> str | None:
    """Return the shared secret, or None when auth is explicitly disabled. Crash early otherwise."""
    if os.environ.get("GYM_AUTH_DISABLED") == "1":
        return None
    token = (os.environ.get("GYM_API_TOKEN") or "").strip()
    if len(token) < 24:
        raise RuntimeError(
            "GYM_API_TOKEN is unset or shorter than 24 characters. Refusing to start "
            "unauthenticated. Set it in .env, or set GYM_AUTH_DISABLED=1 for local development."
        )
    return token


def writes_enabled() -> bool:
    """MCP write tools stay off unless GYM_ALLOW_WRITES=1."""
    return os.environ.get("GYM_ALLOW_WRITES") == "1"


def authorized(header_value: str, token: str) -> bool:
    """True if an Authorization header carries the shared secret (constant-time compare)."""
    scheme, _, rest = (header_value or "").partition(" ")
    scheme, rest = scheme.lower(), rest.strip()
    if scheme == "bearer":
        candidate = rest
    elif scheme == "basic":
        try:
            candidate = base64.b64decode(rest).decode("utf-8").partition(":")[2]
        except Exception:  # noqa: BLE001 — any malformed header is simply unauthorized
            return False
    else:
        return False
    return hmac.compare_digest(candidate.encode(), token.encode())


def install_flask_auth(app, token: str | None):
    """Require the shared secret on every Flask route except EXEMPT_PATHS."""
    if token is None:
        return
    from flask import Response, request

    @app.before_request
    def _require_shared_secret():
        if request.path in EXEMPT_PATHS:
            return None
        if authorized(request.headers.get("Authorization", ""), token):
            return None
        return Response("Authentication required", 401,
                        {"WWW-Authenticate": 'Basic realm="Smart Gym", charset="UTF-8"'})


# Default-deny: every unsafe-method Flask endpoint needs GYM_ALLOW_WRITES=1 UNLESS it is listed here. A new route is
# therefore gated until someone consciously decides it only touches local state. Only list endpoints that never change
# the Speediance account's workouts/schedule/preferences:
#   * pure compute / local files: the coach endpoints, accessories, owned devices, custom instruction
#   * coach.apply: gates its own real saves (dry_run defaults to true and a real save needs writes enabled)
#   * login / settings: (re)establish this app's own session on request of the signed-in user (CSRF-checked)
LOCAL_ONLY_ENDPOINTS = frozenset({
    "features.coach_apply", "features.coach_autoregulate", "features.coach_critique", "features.coach_feedback",
    "features.coach_program", "features.coach_readiness",
    "update_custom_instruction", "update_accessories", "update_owned_devices", "login", "settings",
    "preload_assets", "refresh_library",  # local media/library cache only
})
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def allowed_origins(host: str, forwarded_host: str | None = None, extra: str | None = None) -> set[str]:
    """Hosts a same-origin browser request may name: what the app saw, what a trusted proxy forwarded, and any
    GYM_ALLOWED_ORIGINS entries (comma separated hosts, e.g. gym.lan.doghous.cloud)."""
    hosts = {host}
    if forwarded_host:  # a cross-site page cannot set this header on the victim's browser, so it cannot forge it
        hosts.add(forwarded_host.split(",")[0].strip())
    hosts.update(h.strip() for h in (extra or "").split(","))
    hosts.discard("")  # an empty host would let Origin "null" (which parses to netloc "") through
    return {h.lower() for h in hosts}  # hostnames are case-insensitive; browsers send them lowercase


def cross_site(headers, hosts) -> bool:
    """True if a browser says this request was initiated by another site.

    Browsers attach cached Basic credentials to cross-site requests, so auth alone is not CSRF protection.
    Non-browser clients (curl, MCP) send neither header and are unaffected.
    """
    from urllib.parse import urlparse
    site = headers.get("Sec-Fetch-Site")
    if site and site not in ("same-origin", "none"):
        return True
    origin = headers.get("Origin")
    if not origin:
        return False
    return urlparse(origin).netloc.lower() not in hosts  # Origin "null" (sandboxed iframe, redirect) parses to "" and is refused


def install_flask_write_gate(app):
    """Refuse cross-site unsafe requests, and (default-deny) account-changing endpoints unless GYM_ALLOW_WRITES=1."""
    from flask import jsonify, request

    @app.before_request
    def _write_gate():
        if request.method in SAFE_METHODS:
            return None
        hosts = allowed_origins(request.host, request.headers.get("X-Forwarded-Host"),
                                os.environ.get("GYM_ALLOWED_ORIGINS"))
        if cross_site(request.headers, hosts):
            return jsonify({"error": "cross_site_request_refused"}), 403
        if request.endpoint not in LOCAL_ONLY_ENDPOINTS and not writes_enabled():
            return jsonify({"error": "writes_disabled",
                            "message": "Set GYM_ALLOW_WRITES=1 to let this app change your Speediance account."}), 403
        return None


class AccessJwt:
    """Verifies the `Cf-Access-Jwt-Assertion` header Cloudflare Access adds after it authenticated a user (managed OAuth
    for a remote MCP client such as ChatGPT). Anyone on the LAN can send that header straight to the origin port, so it is
    trusted only after the RS256 signature checks against the team's published keys, plus issuer, audience, expiry and
    an email on the allow-list. A service token (no email) is refused. Every failure means "not authorized"."""

    def __init__(self, team: str, audience: str, emails, key_for=None):
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", team or ""):
            raise RuntimeError("GYM_ACCESS_TEAM must be the Zero Trust team name (letters, digits, dashes)")
        if len(audience or "") < 32:
            raise RuntimeError("GYM_ACCESS_AUD must be the Access application's AUD tag")
        self.emails = {e.strip().lower() for e in emails if e and e.strip()}
        if not self.emails:
            raise RuntimeError("GYM_ACCESS_EMAILS must name at least one allowed email")
        self.issuer = f"https://{team}.cloudflareaccess.com"
        self.audience = audience
        self._key_for = key_for or self._default_key_for()

    FETCH_COOLDOWN_S = 30  # after the key ENDPOINT failed, do not refetch for this long while no keys are cached

    def _default_key_for(self):
        import time
        import jwt
        client = jwt.PyJWKClient(f"{self.issuer}/cdn-cgi/access/certs", cache_keys=True, lifespan=3600, timeout=5)
        state = {"retry_at": 0.0}

        def key_for(token):
            # Only an unreachable endpoint with nothing cached cools down. A token naming an unknown kid must NOT: that is
            # attacker-controlled, and would lock the real user out (PyJWT rate-limits its own unknown-kid refresh).
            cached = client.jwk_set_cache is not None and client.jwk_set_cache.get() is not None
            if not cached and time.monotonic() < state["retry_at"]:
                raise OSError("signing keys unavailable (cooling down)")
            try:
                return client.get_signing_key_from_jwt(token).key
            except jwt.PyJWKClientConnectionError:
                state["retry_at"] = time.monotonic() + self.FETCH_COOLDOWN_S
                raise
        return key_for

    def _plausible(self, token: str) -> bool:
        """Cheap checks on the UNVERIFIED claims, before any key is fetched: the right issuer, audience, an allowed email
        and an unexpired token. A forged token that fails them costs no network call; one that passes still has to verify."""
        import time
        import jwt
        try:
            claims = jwt.decode(token, options={"verify_signature": False})
        except Exception:  # noqa: BLE001
            return False
        aud = claims.get("aud")
        email = claims.get("email")
        exp = claims.get("exp")
        return (claims.get("iss") == self.issuer and (aud == self.audience or (isinstance(aud, list) and self.audience in aud))
                and isinstance(email, str) and email.strip().lower() in self.emails
                and isinstance(exp, (int, float)) and not isinstance(exp, bool) and exp > time.time())

    def email(self, token: str) -> str | None:
        """The verified email of the Access user, or None."""
        import jwt
        if not self._plausible(token):
            return None
        try:
            claims = jwt.decode(token, self._key_for(token), algorithms=["RS256"], audience=self.audience,
                                issuer=self.issuer, options={"require": ["exp", "iat", "iss", "aud"]})
        except Exception:  # noqa: BLE001 — a bad signature, a stale key fetch and an outage all mean "no"
            return None
        email = claims.get("email")
        if not isinstance(email, str) or email.strip().lower() not in self.emails:
            return None
        return email.strip().lower()


def load_access() -> "AccessJwt | None":
    """The Access verifier, or None when GYM_ACCESS_* is unset (then the header is ignored). A partial or malformed
    configuration crashes at start-up rather than silently leaving the door half open."""
    team, aud, emails = (os.environ.get(k, "").strip() for k in ("GYM_ACCESS_TEAM", "GYM_ACCESS_AUD", "GYM_ACCESS_EMAILS"))
    if not (team or aud or emails):
        return None
    return AccessJwt(team, aud, emails.split(","))


class BearerAuthASGI:
    """Pure-ASGI wrapper: 401 unless the request carries the shared secret or a valid Cloudflare Access assertion."""

    def __init__(self, app, token: str, access: "AccessJwt | None" = None):
        self.app = app
        self.token = token
        self.access = access

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            # lifespan must pass through; websockets are not used by this server
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            return await self.app(scope, receive, send)
        if scope.get("path") in EXEMPT_PATHS:
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        value = headers.get(b"authorization", b"").decode("latin-1")
        if authorized(value, self.token):
            return await self.app(scope, receive, send)
        assertion = headers.get(b"cf-access-jwt-assertion", b"").decode("latin-1")
        if self.access is not None and assertion and await asyncio.to_thread(self.access.email, assertion):
            return await self.app(scope, receive, send)
        body = b'{"error":"unauthorized","message":"Missing or invalid bearer token."}'
        await send({"type": "http.response.start", "status": 401, "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (b"www-authenticate", b'Bearer realm="smart-gym-mcp"'),
        ]})
        await send({"type": "http.response.body", "body": body})
