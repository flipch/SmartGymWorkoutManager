"""
Shared-secret authentication for the Smart Gym web UI (Flask) and MCP server (ASGI).

Both services read the same secret from the environment (GYM_API_TOKEN) and fail closed:
if it is unset they refuse to start unless GYM_AUTH_DISABLED=1 is set explicitly.

Accepted credentials:
  * Authorization: Bearer <token>            (agents / MCP clients)
  * Authorization: Basic base64(any:<token>) (browsers; the username is ignored)

/healthz is exempt so Docker and Uptime Kuma keep working.
"""
import base64
import hmac
import os

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


class BearerAuthASGI:
    """Pure-ASGI wrapper: 401 unless the request carries the shared secret."""

    def __init__(self, app, token: str):
        self.app = app
        self.token = token

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
        body = b'{"error":"unauthorized","message":"Missing or invalid bearer token."}'
        await send({"type": "http.response.start", "status": 401, "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (b"www-authenticate", b'Bearer realm="smart-gym-mcp"'),
        ]})
        await send({"type": "http.response.body", "body": body})
