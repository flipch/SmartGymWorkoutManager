"""Read-only Speediance adapter.

Design constraints (from labatt/speediance-api docs/auth.md and our own spike):
  * Logs in as App_type NANO with Versioncode "1". Unlike SOFTWARE (the phone) or HARDWARE (the
    machine), NANO leaves those sessions alone.
  * Token is cached (mode 0600) and reused. We re-login only when the API says the session is gone
    (codes 90/91) and then retry exactly once. There is deliberately NO logout method.
  * Requests are paced (min gap) and never retried blindly: the API throttles bursts and implausible
    version codes ("invalid nonce string").
  * Failures raise (SourceDown / SourceAuthError / SchemaDrift). An empty list means "the API
    answered and there are no sessions", never "the call failed".
  * The unit is NOT inferred: the NANO login response reports weightUnit=0 even for imperial accounts.
"""
import json
import pathlib
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from . import localsecrets as secrets
from .errors import SchemaDrift, SourceAuthError, SourceDown
from .localday import DEFAULT_TZ

SOURCE = "speediance"
APP_TYPE = "NANO"
VERSIONCODE = "1"
SESSION_GONE_CODES = (90, 91)
HOSTS = {"Global": "api2.speediance.com", "EU": "euapi.speediance.com"}
MOBILE_DEVICE = {"brand": "google", "device": "emulator64_x86_64_arm64", "deviceType": "sdk_gphone64_x86_64",
                 "os": "", "os_version": "31", "manufacturer": "Google"}

# history record `type` -> detail endpoint family (mirrors features.TRAINING_TYPE_MAP; type 1/7 have no per-set detail)
DETAIL_ENDPOINT = {2: "courseTrainingInfoDetail", 5: "cttTrainingInfoDetail"}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """The session Token travels in a header: never replay it to wherever a redirect points."""

    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def default_transport(method: str, url: str, headers: dict, body: dict | None):
    """(status, parsed-json) via urllib, never following redirects. Network errors raise OSError, which the caller wraps."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _OPENER.open(req, timeout=20) as resp:
            status, raw = resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        status, raw = err.code, err.read().decode("utf-8", "replace")
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, {"_non_json": raw[:120]}


class SpeedianceNano:
    def __init__(self, creds_path=None, token_path=None, region="Global", tz=DEFAULT_TZ,
                 transport=default_transport, clock=time.time, sleep=time.sleep, min_gap_s=1.2):
        self.creds_path = pathlib.Path(creds_path or secrets.CONFIG_DIR / "creds.env")
        self.token_path = pathlib.Path(token_path or secrets.CONFIG_DIR / "nano.token.json")
        self.host = HOSTS.get(region, HOSTS["Global"])
        self.base = "https://" + self.host
        self.tz = tz
        self._transport, self._clock, self._sleep, self._min_gap = transport, clock, sleep, min_gap_s
        self._last_call = 0.0
        self._session = None  # {"token":..., "user_id":...}
        self.logins = 0  # observable, so tests and the recorder can prove we did not churn sessions

    # ── plumbing ────────────────────────────────────────────────────────────
    def _headers(self, with_session: bool) -> dict:
        now = self._clock()
        local = datetime.fromtimestamp(now, tz=timezone.utc).astimezone(ZoneInfo(self.tz))
        seconds = int(local.utcoffset().total_seconds())
        sign = "+" if seconds >= 0 else "-"
        seconds = abs(seconds)
        headers = {
            "Host": self.host, "User-Agent": "Dart/3.9 (dart:io)", "Content-Type": "application/json",
            "Timestamp": str(int(now * 1000)), "Versioncode": VERSIONCODE, "App_type": APP_TYPE,
            "Accept-Language": "en", "Timezone": self.tz,
            "Utc_offset": f"{sign}{seconds // 3600:02d}{(seconds % 3600) // 60:02d}",
            "Mobiledevices": json.dumps(MOBILE_DEVICE),
        }
        if with_session:
            headers["Token"] = self._session["token"]
            headers["App_user_id"] = str(self._session["user_id"])
        return headers

    def _pace(self):
        wait = self._min_gap - (self._clock() - self._last_call)
        if wait > 0:
            self._sleep(wait)
        self._last_call = self._clock()

    def _call(self, method, path, body=None, with_session=True):
        self._pace()
        try:
            status, payload = self._transport(method, self.base + path, self._headers(with_session), body)
        except OSError as err:
            raise SourceDown(SOURCE, f"network error: {type(err).__name__}") from err
        if not isinstance(payload, dict):
            raise SchemaDrift(SOURCE, "response was not a JSON object")
        code = payload.get("code")
        if status == 401 or code in SESSION_GONE_CODES:
            raise SourceAuthError(SOURCE, f"session rejected (http={status}, code={code})")
        if status != 200 or code != 0:
            raise SourceDown(SOURCE, f"http={status} code={code} message={str(payload.get('message'))[:80]!r}")
        return payload

    def _login(self):
        env = secrets.load_env_file(self.creds_path)
        email, password = env.get("SPEEDIANCE_EMAIL"), env.get("SPEEDIANCE_PASSWORD")
        if not email or not password:
            raise SourceDown(SOURCE, f"fill SPEEDIANCE_EMAIL / SPEEDIANCE_PASSWORD in {self.creds_path}")
        ident = self._call("POST", "/api/app/v2/login/verifyIdentity", {"type": 2, "userIdentity": email}, False)
        data = ident.get("data") or {}
        if data.get("isExist") is False or data.get("hasPwd") is False:
            raise SourceDown(SOURCE, "account missing or has no password set")
        done = self._call("POST", "/api/app/v2/login/byPass",
                          {"userIdentity": email, "password": password, "type": 2}, False)
        info = done.get("data") or {}
        if not info.get("token") or not info.get("appUserId"):
            raise SchemaDrift(SOURCE, "login response lacked token/appUserId")
        self.logins += 1
        self._session = {"token": info["token"], "user_id": info["appUserId"], "saved_at": int(self._clock())}
        secrets.write_json_private(self.token_path, self._session)

    def _ensure_session(self):
        if self._session is None:
            self._session = secrets.read_json_private(self.token_path)
        if self._session is None:
            self._login()

    def _get(self, path):
        """GET with the cached session; on a rejected session re-login once and retry once."""
        self._ensure_session()
        try:
            return self._call("GET", path)
        except SourceAuthError:
            self._session = None
            self._login()
            return self._call("GET", path)

    # ── reads ───────────────────────────────────────────────────────────────
    def history(self, start: date, end: date) -> list[dict]:
        """Completed-session records between two dates (inclusive). [] means genuinely none."""
        payload = self._get(f"/api/mobile/v2/report/userTrainingDataRecord?startDate={start}&endDate={end}")
        data = payload.get("data")
        if not isinstance(data, list):
            raise SchemaDrift(SOURCE, "history `data` was not a list")
        return data

    def detail(self, training_id: int, record_type: int):
        """Per-exercise, per-set detail for one completed session. Raises for types without detail."""
        endpoint = DETAIL_ENDPOINT.get(record_type)
        if endpoint is None:
            raise SourceDown(SOURCE, f"no per-set detail for record type {record_type}")
        return self._get(f"/api/app/trainingInfo/{endpoint}/{int(training_id)}").get("data")
