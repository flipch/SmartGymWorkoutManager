"""
whoop_sync.py — the gym app pulls WHOOP itself (web and MCP are separate processes on one DATA_DIR volume).

It reuses harness/whoop.py (parsers, `TokenStore`) and harness/whoop_client.py (HTTP, OAuth) instead of rewriting them:

  * credentials: env WHOOP_CLIENT_ID + WHOOP_CLIENT_SECRET, or a mode-0600 file named by WHOOP_CREDS_FILE. No default
    under ~/.config: a server must be told where its secrets are.
  * tokens: DATA_DIR/whoop.token.json (mode 0600), or WHOOP_TOKEN_FILE. WHOOP rotates BOTH tokens on every refresh, so
    refreshes go through TokenStore's OS file lock: the web process and the MCP process can never refresh at once, and the
    loser reuses the winner's tokens. Nothing else in this module touches the tokens.
  * pulls are throttled (WHOOP_PULL_MIN_INTERVAL_S, default 900), time-boxed (WHOOP_HTTP_TIMEOUT_S, default 5) and back
    off after a failure. A failure never raises into a page: it becomes a status. A dead refresh token is the
    `needs_reconsent` state: no more retries until the owner reconnects.
  * only SCORED readings count (the parsers return nothing for pending/unscorable, never 0), and each field is stamped with
    when it was MEASURED, so a repeated pull cannot make a day-old score look fresh.
  * a token, secret or authorization code is never logged, returned, or rendered; messages carry a status or a class name.

Progress state (no secrets) is DATA_DIR/whoop_state.json.
"""
import hashlib
import hmac
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests

import state_io
from harness import whoop_client as wc
from harness.errors import SchemaDrift, SourceAuthError, SourceDown
from harness.whoop import TokenStore

DEFAULT_INTERVAL_S = 900
DEFAULT_TIMEOUT_S = 5.0
FORCE_FLOOR_S = 30  # the "Refresh" button cannot hammer WHOOP either
BACKOFF_BASE_S = 120
BACKOFF_MAX_S = 3600
CONSENT_TTL_S = 600
CALLBACK_PATH = "/whoop/callback"
MAX_PENDING_STATES = 5
_TOKENISH = re.compile(r"[A-Za-z0-9._~\-]{1,2048}")  # what an OAuth code or our state can look like: anything else is refused unread
SYNCED_KINDS = ("whoop_recovery", "sleep_hours")  # all WHOOP yields: no HRV/resting-HR baseline
HOW_TO = ("WHOOP is not connected. Set WHOOP_CLIENT_ID and WHOOP_CLIENT_SECRET on the server and put the consented "
          "whoop.token.json in the data volume (see docs/ai-coach.md). The coach works without it.")
RECONSENT = "WHOOP rejected the saved sign-in. Reconnect WHOOP to resume; the last readings stay as they were."


# ------------------------------------------------------------------------------------------------ configuration
def _env(env):
    return os.environ if env is None else env


def interval_s(env=None):
    return _number(_env(env).get("WHOOP_PULL_MIN_INTERVAL_S"), DEFAULT_INTERVAL_S, 60, 86400)


def timeout_s(env=None):
    return _number(_env(env).get("WHOOP_HTTP_TIMEOUT_S"), DEFAULT_TIMEOUT_S, 1, 15)


def _number(raw, default, low, high):
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if low <= value <= high else default  # junk or out of range falls back, never crashes a page


def token_path(data_dir, env=None):
    return (_env(env).get("WHOOP_TOKEN_FILE") or "").strip() or os.path.join(data_dir, "whoop.token.json")


def load_creds(env=None):
    """The app credentials or None. Raises nothing secret: a malformed value is reported as 'unusable'."""
    env = _env(env)
    cid, secret = (env.get("WHOOP_CLIENT_ID") or "").strip(), (env.get("WHOOP_CLIENT_SECRET") or "").strip()
    if cid and secret:
        if len(cid) > 200 or len(secret) > 200 or re.search(r"\s", cid + secret):
            raise ValueError("WHOOP_CLIENT_ID / WHOOP_CLIENT_SECRET are not usable (whitespace or too long)")
        return wc.AppCredentials(cid, secret)
    path = (env.get("WHOOP_CREDS_FILE") or "").strip()
    if path:
        try:
            return wc.load_app_credentials(path)
        except (OSError, ValueError) as err:
            raise ValueError(f"WHOOP_CREDS_FILE is not usable ({type(err).__name__})") from None
    return None


def redirect_uri(env=None):
    """WHOOP_REDIRECT_URI when it is a usable callback for THIS app, else None."""
    raw = (_env(env).get("WHOOP_REDIRECT_URI") or "").strip()
    if not raw:
        return None
    u = urlparse(raw)
    local = u.hostname in ("localhost", "127.0.0.1", "::1")
    if (u.scheme == "https" or (u.scheme == "http" and local)) and u.hostname and not u.username and not u.query \
            and not u.fragment and u.path == CALLBACK_PATH:
        return raw
    return None


class _TimedHttp:
    """requests with a hard per-call timeout (the harness default is 15 s: too long for a page)."""

    def __init__(self, timeout, inner=requests):
        self.timeout, self.inner = timeout, inner

    def get(self, url, **kw):
        kw["timeout"] = min(kw.get("timeout") or self.timeout, self.timeout)
        return self.inner.get(url, **kw)

    def post(self, url, **kw):
        kw["timeout"] = min(kw.get("timeout") or self.timeout, self.timeout)
        return self.inner.post(url, **kw)


# ------------------------------------------------------------------------------------------------ progress state
def _state_path(data_dir):
    return os.path.join(data_dir, "whoop_state.json")


def _load_state(data_dir):
    import json
    try:
        with open(_state_path(data_dir)) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}  # progress only: a damaged file means "no history", the next pull rewrites it


def _save_state(data_dir, state):
    state_io.atomic_write_json(_state_path(data_dir), state)


def _safe_text(value, limit=160):
    return re.sub(r"[^\x20-\x7e]", "", str(value))[:limit]


def _stamp(dt):
    return state_io.utc_stamp(dt)


def _when(text):
    return state_io.parse_stamp(text)


# ------------------------------------------------------------------------------------------------ status
def status(data_dir, now=None, env=None):
    """What the Coach page and the MCP readiness tool show. Never contains a token, secret or path."""
    now = now or datetime.now(timezone.utc)
    env = _env(env)
    try:
        creds = load_creds(env)
        creds_error = None
    except ValueError as err:
        creds, creds_error = None, _safe_text(err)
    has_tokens = os.path.exists(token_path(data_dir, env))
    state = _load_state(data_dir)
    err = state.get("error") if isinstance(state.get("error"), dict) else None
    if creds is None or not has_tokens:
        kind = "not_configured"
    elif state.get("needs_reconsent") is True:
        kind = "needs_reconsent"
    elif err:
        kind = "refreshing_failed"
    else:
        kind = "connected"
    last_success, next_allowed = _when(state.get("last_success")), _when(state.get("next_allowed"))
    out = {"state": kind,
           "configured": kind != "not_configured",
           "can_connect": bool(creds and redirect_uri(env)),
           "last_pull": state.get("last_success") if last_success else None,
           "last_attempt": state.get("last_attempt") if _when(state.get("last_attempt")) else None,
           "next_pull": state.get("next_allowed") if next_allowed and kind in ("connected", "refreshing_failed") else None,
           "interval_s": interval_s(env),
           "failures": state.get("failures") if isinstance(state.get("failures"), int) else 0,
           "message": None,
           "notes": [_safe_text(n) for n in (state.get("notes") or []) if isinstance(n, str)][:5]}
    if kind == "not_configured":
        if creds is not None:  # the app is registered but nothing has consented yet: the next step is different
            out["message"] = ("WHOOP credentials are set but nothing is connected yet. " + ("Use Connect WHOOP to approve access, or p" if out["can_connect"] else "P")
                              + "ut the consented whoop.token.json in the data volume.")
        else:
            out["message"] = creds_error or HOW_TO
    elif kind == "needs_reconsent":
        out["message"] = RECONSENT
    elif kind == "refreshing_failed":
        out["message"] = _safe_text(err.get("message") or "WHOOP unavailable")
    return out


# ------------------------------------------------------------------------------------------------ the pull
def _default_save(payload, data_dir, measured_at):
    import features  # imported late: features imports this module for its routes
    return features.save_readiness_report(payload, data_dir, measured_at=measured_at)


def _result(outcome, data_dir, env, now, **extra):
    return {"outcome": outcome, "status": status(data_dir, now, env), **extra}


def sync(data_dir, *, force=False, now=None, env=None, http=None, save=None, client_factory=None):
    """Pull WHOOP if it is due and store what is scored. Returns {"outcome", "status", ...}; it never raises for a WHOOP
    problem. Outcomes: pulled | throttled | busy | not_configured | needs_reconsent | failed."""
    now = now or datetime.now(timezone.utc)
    env = _env(env)
    try:
        creds = load_creds(env)
    except ValueError:
        creds = None
    path = token_path(data_dir, env)
    if creds is None or not os.path.exists(path):
        return _result("not_configured", data_dir, env, now)
    try:
        lock = state_io.file_lock(os.path.join(data_dir, "whoop_pull.lock"), timeout=0.3)
        with lock:
            state = _load_state(data_dir)  # re-read under the lock: the other process may have pulled while we waited
            if state.get("needs_reconsent") is True:
                return _result("needs_reconsent", data_dir, env, now)
            last_attempt, due = _when(state.get("last_attempt")), _when(state.get("next_allowed"))
            if force:
                if last_attempt and (now - last_attempt).total_seconds() < FORCE_FLOOR_S:
                    return _result("throttled", data_dir, env, now)
            elif due and now < due:
                return _result("throttled", data_dir, env, now)
            state["last_attempt"] = _stamp(now)
            state["next_allowed"] = _stamp(now + timedelta(seconds=BACKOFF_BASE_S))  # a crash mid-pull still backs off
            _save_state(data_dir, state)
            return _pull_locked(data_dir, state, creds, path, now, env, http, save, client_factory)
    except TimeoutError:
        return _result("busy", data_dir, env, now)
    except OSError as err:  # a read-only or full volume must not break a page either
        return _result("failed", data_dir, env, now, error=_safe_text(type(err).__name__))


def _fail(data_dir, state, now, env, kind, message):
    failures = (state.get("failures") if isinstance(state.get("failures"), int) else 0) + 1
    state["failures"] = failures
    state["error"] = {"kind": kind, "message": _safe_text(message)}
    if kind == "needs_reconsent":
        state["needs_reconsent"] = True
        state["next_allowed"] = None
        state["error"]["message"] = RECONSENT
    else:
        delay = min(BACKOFF_MAX_S, BACKOFF_BASE_S * 2 ** (failures - 1))
        state["next_allowed"] = _stamp(now + timedelta(seconds=delay))
    _save_state(data_dir, state)
    return _result("needs_reconsent" if kind == "needs_reconsent" else "failed", data_dir, env, now)


def _pull_locked(data_dir, state, creds, path, now, env, http, save, client_factory):
    http = _TimedHttp(timeout_s(env), http or requests) if not isinstance(http, _TimedHttp) else http
    try:
        client = (client_factory or wc.WhoopClient)(creds, TokenStore(path), http=http)
        readings, notes = client.fetch_readings()
    except SourceAuthError:
        return _fail(data_dir, state, now, env, "needs_reconsent", RECONSENT)
    except SchemaDrift as err:
        return _fail(data_dir, state, now, env, "unavailable", f"WHOOP answered in an unexpected shape ({err})")
    except SourceDown as err:
        return _fail(data_dir, state, now, env, "unavailable", f"WHOOP unavailable: {err}")
    except Exception as err:  # noqa: BLE001 - never a stack trace on a page
        return _fail(data_dir, state, now, env, "unavailable", f"WHOOP unavailable: {type(err).__name__}")
    fresh = [r for r in readings if r.kind in SYNCED_KINDS and isinstance(r.value, (int, float))
             and not isinstance(r.value, bool)]
    saved, skipped = [], {}
    if fresh:
        payload = {r.kind: r.value for r in fresh}
        payload["source"] = "whoop"
        try:
            _stored, skipped = (save or _default_save)(payload, data_dir, {r.kind: r.as_of for r in fresh})
        except ValueError as err:  # the coach's own validation refused a WHOOP value: surface the field, not the value
            return _fail(data_dir, state, now, env, "unavailable", f"WHOOP sent a value the coach refused ({err})")
        saved = [r.kind for r in fresh if r.kind not in skipped]
    state.update({"last_success": _stamp(now), "failures": 0, "error": None, "needs_reconsent": False,
                  "next_allowed": _stamp(now + timedelta(seconds=interval_s(env))),
                  "notes": [_safe_text(n) for n in notes][:5]})
    _save_state(data_dir, state)
    return _result("pulled", data_dir, env, now, saved=saved, skipped=skipped)


# ------------------------------------------------------------------------------------------------ in-app consent
# The consent link can be minted by the web app (Coach page button) or by the MCP process (whoop_connect), and the callback is
# received by the web app, so the pending states live in the shared DATA_DIR: atomic file + OS lock, bounded in number.
# Login-CSRF (an attacker making the owner's server store the ATTACKER's WHOOP account) is prevented by the single-use,
# unguessable state: the callback does nothing unless it carries a state that an authenticated caller minted moments ago.
class ConsentError(Exception):
    """Carries a message that is safe to show: never the code, the state or a token."""


def _consent_path(data_dir):
    return os.path.join(data_dir, "whoop_consent.json")


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _pending(data_dir, now):
    import json
    try:
        with open(_consent_path(data_dir)) as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return []
    items = raw.get("pending") if isinstance(raw, dict) else None
    out = []
    for it in items if isinstance(items, list) else []:
        exp = _when(it.get("expires")) if isinstance(it, dict) else None
        if exp and exp > now and isinstance(it.get("hash"), str):
            out.append({"hash": it["hash"], "expires": it["expires"]})
    return out


def begin_consent(data_dir, env=None, now=None):
    """(authorize_url, state): a fresh single-use state (192 bits, valid 10 minutes, at most MAX_PENDING_STATES pending)."""
    env = _env(env)
    uri = redirect_uri(env)
    try:
        creds = load_creds(env)
    except ValueError:
        creds = None
    if creds is None or uri is None:
        raise ConsentError("Connecting WHOOP needs WHOOP_CLIENT_ID, WHOOP_CLIENT_SECRET and WHOOP_REDIRECT_URI on the server.")
    now = now or datetime.now(timezone.utc)
    state = secrets.token_urlsafe(24)
    with state_io.file_lock(_consent_path(data_dir) + ".lock"):
        pending = _pending(data_dir, now)
        pending.append({"hash": _hash(state), "expires": _stamp(now + timedelta(seconds=CONSENT_TTL_S))})
        state_io.atomic_write_json(_consent_path(data_dir), {"pending": pending[-MAX_PENDING_STATES:]})
    return wc.authorize_url(creds, state, uri), state


def _burn(data_dir, state, now):
    """True when `state` was pending and unexpired; it is removed either way (single use)."""
    with state_io.file_lock(_consent_path(data_dir) + ".lock"):
        pending = _pending(data_dir, now)
        digest = _hash(state)
        match = [p for p in pending if hmac.compare_digest(p["hash"], digest)]
        if match:
            state_io.atomic_write_json(_consent_path(data_dir), {"pending": [p for p in pending if p not in match]})
        return bool(match)


def finish_consent(data_dir, code, state, env=None, http=None, now=None):
    """Exchange the code (server side), write the tokens 0600 through the TokenStore, clear the re-consent flag.
    Nothing happens unless `state` is a pending, unexpired one; it is burned before the exchange."""
    env = _env(env)
    now = now or datetime.now(timezone.utc)
    if not (isinstance(code, str) and isinstance(state, str) and _TOKENISH.fullmatch(code) and _TOKENISH.fullmatch(state)):
        raise ConsentError("This sign-in link is not valid. Start again from the Coach page.")
    if not _burn(data_dir, state, now):
        raise ConsentError("This sign-in link is unknown, expired or already used. Start again from the Coach page.")
    try:
        creds, uri = load_creds(env), redirect_uri(env)
    except ValueError:
        creds, uri = None, None
    if creds is None or uri is None:
        raise ConsentError("WHOOP is not configured on the server.")
    try:
        tokens = wc.exchange_code(creds, code, _TimedHttp(timeout_s(env), http or requests), time.time, uri)
        TokenStore(token_path(data_dir, env)).save(tokens)  # 0600, atomic, behind the single-refresher lock
    except SourceDown as err:
        raise ConsentError(f"WHOOP did not accept the sign-in: {_safe_text(err)}") from None
    except OSError:
        raise ConsentError("WHOOP accepted the sign-in but the server could not save the tokens (check the data volume).") from None
    with state_io.file_lock(os.path.join(data_dir, "whoop_pull.lock"), timeout=5):
        st = _load_state(data_dir)
        st.update({"needs_reconsent": False, "error": None, "failures": 0, "next_allowed": None})
        _save_state(data_dir, st)


def connect_info(data_dir, force=False, env=None, now=None):
    """What the MCP whoop_connect tool answers: the status, and a one-time link only when WHOOP needs one (or force)."""
    st = status(data_dir, now, env)
    needs = st["state"] in ("not_configured", "needs_reconsent")
    out = {"state": st["state"], "whoop": st, "connect_url": None, "expires_in_s": None}
    if not (needs or force):
        out["message"] = "WHOOP is " + ("connected." if st["state"] == "connected" else "connected but the last pull failed; it retries by itself.") \
            + " No link was created. Pass force=true only if you really want to reconnect."
        return out
    if not st["can_connect"]:
        out["message"] = ("The server cannot start a WHOOP sign-in: set WHOOP_CLIENT_ID, WHOOP_CLIENT_SECRET and WHOOP_REDIRECT_URI "
                          "(https://<your host>/whoop/callback) on the server.")
        return out
    url, _state = begin_consent(data_dir, env, now)
    out["connect_url"] = url
    out["expires_in_s"] = CONSENT_TTL_S
    out["message"] = "Open this link in a browser on your home network or Tailscale and approve; it works once and expires in 10 minutes."
    return out
