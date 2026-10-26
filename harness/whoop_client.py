"""WHOOP v2 HTTP client + the one-time OAuth consent. Read-only; least privilege.

Pattern: OAuth 2.0 authorization-code grant with a loopback redirect (RFC 8252 section 7.3) and a `state` check
(RFC 6749 section 10.12). Tokens live in the existing mode-0600 `TokenStore`, which already serialises WHOOP's
rotating refresh (a refresh invalidates BOTH tokens, so two refreshers would kill each other).

Scopes are the minimum the planner uses: `offline` (refresh token), `read:recovery`, `read:sleep`. The registered app
may allow more; we never ask for them.

Secrets: the client secret and tokens never appear in an exception message, a log line or a return value shown to the
user. Error text carries an HTTP status and a class name only.
"""
import hmac
import html
import http.server
import pathlib
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from . import localsecrets
from .errors import SchemaDrift, SourceAuthError, SourceDown
from .whoop import SOURCE, TOKEN_URL, TokenStore, parse_recovery, parse_sleep

API = "https://api.prod.whoop.com/developer"
AUTH_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"
SCOPES = ("offline", "read:recovery", "read:sleep")
REQUIRED_GRANTED = ("offline", "read:recovery", "read:sleep")
REDIRECT_URI = "http://localhost:8000/callback"
DEFAULT_CREDS = localsecrets.CONFIG_DIR / "whoop.txt"
DEFAULT_TOKENS = localsecrets.CONFIG_DIR / "whoop.token.json"
HTTP_TIMEOUT_S = 15
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_SECRET_LINE = re.compile(r"\S{64}")
_MASKED = re.compile(r"[*\u2022\u25cf\u00b7]{8,}")  # the dashboard hides the secret behind a row of these


@dataclass(frozen=True)
class AppCredentials:
    client_id: str
    client_secret: str = field(repr=False)  # never printed, even by a stray repr()

    def __repr__(self) -> str:
        return "AppCredentials(client_id=<redacted>, client_secret=<redacted>)"


_MASKED_MSG = ("{name}: the client secret is the masked placeholder (a row of asterisks) from the dashboard, not the "
               "real secret; copy the real value from the WHOOP developer dashboard (it is shown once, or regenerate it)")


def load_app_credentials(path=DEFAULT_CREDS) -> AppCredentials:
    """Read the app id/secret from a mode-0600 file: either KEY=VALUE (WHOOP_CLIENT_ID / WHOOP_CLIENT_SECRET) or the
    text pasted from the WHOOP dashboard (one UUID line = id, one 64-character token line = secret)."""
    path = pathlib.Path(path)
    if not path.exists():
        raise FileNotFoundError(f"missing {path}")
    if path.stat().st_mode & 0o077:
        raise PermissionError(f"{path} must be chmod 600")
    text = path.read_text()
    kv = {}
    for line in text.splitlines():
        if "=" in line and not line.lstrip().startswith(("#", "http")):
            key, value = line.split("=", 1)
            kv[key.strip().upper()] = value.strip().strip("\"'")
    if _MASKED.fullmatch(kv.get("WHOOP_CLIENT_SECRET", "")):
        raise ValueError(_MASKED_MSG.format(name=path.name))
    if kv.get("WHOOP_CLIENT_ID") and kv.get("WHOOP_CLIENT_SECRET"):
        return AppCredentials(kv["WHOOP_CLIENT_ID"], kv["WHOOP_CLIENT_SECRET"])
    lines = [ln.strip() for ln in text.splitlines()]
    ids = [ln for ln in lines if _UUID.fullmatch(ln)]
    secrets_found = [ln for ln in lines if _SECRET_LINE.fullmatch(ln) and "://" not in ln and not _MASKED.fullmatch(ln)]
    if not secrets_found and any(_MASKED.fullmatch(ln) for ln in lines):
        raise ValueError(_MASKED_MSG.format(name=path.name))
    if len(ids) != 1 or len(secrets_found) != 1:
        raise ValueError(f"{path.name}: expected exactly one client id (UUID) and one 64-character client secret, "
                         f"found {len(ids)} and {len(secrets_found)}")
    return AppCredentials(ids[0], secrets_found[0])


def authorize_url(creds: AppCredentials, state: str) -> str:
    return AUTH_URL + "?" + urlencode({"response_type": "code", "client_id": creds.client_id,
                                       "redirect_uri": REDIRECT_URI, "scope": " ".join(SCOPES), "state": state})


def _token_request(http, data: dict, what: str) -> dict:
    try:
        resp = http.post(TOKEN_URL, data=data, timeout=HTTP_TIMEOUT_S, allow_redirects=False)
    except requests.RequestException as err:
        raise SourceDown(SOURCE, f"{what}: network error ({type(err).__name__})") from None
    if resp.status_code in (400, 401, 403):
        raise SourceAuthError(SOURCE, f"{what} rejected (HTTP {resp.status_code}): re-authorise WHOOP")
    if resp.status_code != 200:
        raise SourceDown(SOURCE, f"{what} failed (HTTP {resp.status_code})")
    try:
        body = resp.json()
    except ValueError:
        raise SchemaDrift(SOURCE, f"{what}: response was not JSON") from None
    if not isinstance(body, dict):
        raise SchemaDrift(SOURCE, f"{what}: response was not an object")
    return body


def _keep(body: dict, now: float) -> dict:
    """The fields we store. Anything else WHOOP returns (e.g. an id_token) is dropped."""
    kept = {k: body[k] for k in ("access_token", "refresh_token", "expires_in", "scope", "token_type") if k in body}
    kept["obtained_at"] = int(now)
    return kept


def exchange_code(creds: AppCredentials, code: str, http=requests, now=time.time) -> dict:
    body = _token_request(http, {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI,
                                 "client_id": creds.client_id, "client_secret": creds.client_secret},
                          "authorization-code exchange")
    granted = set(str(body.get("scope", "")).split())
    missing = [s for s in REQUIRED_GRANTED if s not in granted]
    if missing:
        raise SourceAuthError(SOURCE, f"consent did not grant: {', '.join(missing)}")
    return _keep(body, now())


def refresh_tokens(creds: AppCredentials, refresh_token: str, http=requests, now=time.time) -> dict:
    body = _token_request(http, {"grant_type": "refresh_token", "refresh_token": refresh_token,
                                 "client_id": creds.client_id, "client_secret": creds.client_secret,
                                 "scope": "offline"}, "token refresh")
    return _keep(body, now())


# ------------------------------------------------------------------------------------------- one-time consent
class _CallbackServer(http.server.HTTPServer):
    allow_reuse_address = True
    expected_state: str = ""
    result: dict | None = None


class _Callback(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):  # the query string holds the one-time code: no request logging
        pass

    def _reply(self, status: int, text: str):
        body = f"<!doctype html><meta charset=utf-8><title>WHOOP</title><p>{html.escape(text)}</p>".encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path != "/callback":
            return self._reply(404, "Not found.")
        query = parse_qs(url.query)
        got_state = (query.get("state") or [""])[0]
        if not hmac.compare_digest(got_state.encode(), self.server.expected_state.encode()):
            return self._reply(400, "State mismatch: this request was not started by this program.")
        if "error" in query:
            self.server.result = {"error": (query["error"] or ["unknown"])[0][:40]}
            return self._reply(200, "WHOOP authorisation was declined. You can close this tab.")
        code = (query.get("code") or [""])[0]
        if not code:
            return self._reply(400, "Missing code.")
        self.server.result = {"code": code}
        self._reply(200, "Authorisation received. Return to the terminal: it says whether the token exchange worked.")


def run_consent(creds: AppCredentials, store: TokenStore, *, open_url, port: int = 8000, timeout_s: float = 180,
                http=requests, now=time.time) -> dict:
    """Open the consent page, wait for the loopback redirect, exchange the code, store the tokens. Returns the
    (token-free) summary {"scope": ..., "expires_in": ...}."""
    state = secrets.token_urlsafe(24)
    server = _CallbackServer(("127.0.0.1", port), _Callback)
    server.expected_state = state
    server.timeout = 0.25
    try:
        open_url(authorize_url(creds, state))
        deadline = time.monotonic() + timeout_s
        while server.result is None and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    result = server.result
    if result is None:
        raise SourceDown(SOURCE, f"no consent received within {timeout_s:g} s")
    if "error" in result:
        raise SourceAuthError(SOURCE, f"consent declined ({result['error']})")
    tokens = exchange_code(creds, result["code"], http, now)
    store.save(tokens)
    return {"scope": tokens.get("scope"), "expires_in": tokens.get("expires_in")}


# ------------------------------------------------------------------------------------------- reads
class WhoopClient:
    def __init__(self, creds: AppCredentials, store: TokenStore, http=requests, now=time.time):
        self.creds, self.store, self.http, self.now = creds, store, http, now

    def _tokens(self) -> dict:
        tokens = self.store.load()
        if not tokens or not tokens.get("access_token") or not tokens.get("refresh_token"):
            raise SourceAuthError(SOURCE, "WHOOP is not authorised: run `python -m harness.whoop_auth`")
        return tokens

    def _get_once(self, path: str, params: dict, access_token: str):
        try:
            return self.http.get(API + path, params=params, headers={"Authorization": f"Bearer {access_token}"},
                                 timeout=HTTP_TIMEOUT_S, allow_redirects=False)
        except requests.RequestException as err:
            raise SourceDown(SOURCE, f"network error ({type(err).__name__})") from None

    def get(self, path: str, params: dict) -> dict:
        tokens = self._tokens()
        resp = self._get_once(path, params, tokens["access_token"])
        if resp.status_code == 401:  # access token expired: rotate once (cross-process safe), then retry once
            fresh = self.store.refresh_if_still_stale(
                tokens["refresh_token"], lambda rt: refresh_tokens(self.creds, rt, self.http, self.now))
            resp = self._get_once(path, params, fresh["access_token"])
        if resp.status_code == 401 or resp.status_code == 403:
            raise SourceAuthError(SOURCE, f"{path} rejected (HTTP {resp.status_code}): re-authorise WHOOP")
        if resp.status_code == 429:
            retry = resp.headers.get("Retry-After", "?")
            raise SourceDown(SOURCE, f"rate limited (Retry-After {str(retry)[:10]})")
        if resp.status_code != 200:
            raise SourceDown(SOURCE, f"{path} failed (HTTP {resp.status_code})")
        try:
            body = resp.json()
        except ValueError:
            raise SchemaDrift(SOURCE, f"{path}: response was not JSON") from None
        if not isinstance(body, dict):
            raise SchemaDrift(SOURCE, f"{path}: response was not an object")
        return body

    def fetch_readings(self):
        """(readings, notes) from the newest recovery and the newest night's sleep. Raises SourceDown/AuthError."""
        recovery = self.get("/v2/recovery", {"limit": 3})
        sleep = self.get("/v2/activity/sleep", {"limit": 10})  # a night can be split across several records
        readings, notes = parse_recovery(recovery)
        more, more_notes = parse_sleep(sleep)
        return readings + more, notes + more_notes


def default_client(creds_path=DEFAULT_CREDS, token_path=DEFAULT_TOKENS) -> WhoopClient:
    return WhoopClient(load_app_credentials(creds_path), TokenStore(token_path))
