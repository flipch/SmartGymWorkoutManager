"""WHOOP API v2: parsing + safe token rotation. Deliberately NO HTTP layer yet.

Why no HTTP client: it cannot be verified until Felipe registers a WHOOP developer app (Sandbox tier, redirect
URL), and an untested client would only prove the code agrees with itself. What IS verifiable now is below:
  * parsers validated against WHOOP's own published OpenAPI spec (tests/fixtures/whoop_openapi.json, from
    https://api.prod.whoop.com/developer/doc/openapi.json, fetched 2026-10-26);
  * the rotating-token refresh protocol: WHOOP rotates BOTH tokens on refresh, so two concurrent refreshers
    invalidate each other. TokenStore serializes refreshes behind an OS file lock and lets the loser reuse the
    winner's tokens.
Endpoints (spec): GET /v2/recovery, /v2/activity/sleep, /v2/cycle (collections: {records, next_token}); OAuth token
endpoint per WHOOP docs: https://api.prod.whoop.com/oauth/oauth2/token (unverified live).

Semantics enforced here (from the spec + research):
  * only score_state == SCORED counts; PENDING_SCORE / UNSCORABLE yield NO reading (never "0") so the planner's
    freshness gate decides; a user still calibrating yields no recovery reading either;
  * naps are ignored for sleep_hours; sleep_hours counts light + slow-wave + REM time asleep;
  * WHOOP does not expose a resting-HR/HRV baseline, so we do not derive rhr/hrv signals here (recovery already
    encodes them, and unvalidated thresholds would only add noise).
"""
import fcntl
import os
import pathlib
from datetime import datetime

from . import localsecrets
from .errors import SchemaDrift, SourceAuthError, SourceDown
from .model import Reading

SOURCE = "whoop"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
MS_PER_HOUR = 3_600_000


def _parse_ts(value: str, what: str) -> datetime:
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as err:
        raise SchemaDrift(SOURCE, f"{what} is not an ISO-8601 timestamp") from err
    if ts.tzinfo is None:
        raise SchemaDrift(SOURCE, f"{what} has no timezone")
    return ts


def _records(payload) -> list[dict]:
    if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
        raise SchemaDrift(SOURCE, "collection payload lacked a `records` list")
    return payload["records"]


def _newest(records: list[dict], keep) -> dict | None:
    candidates = [r for r in records if keep(r)]
    return max(candidates, key=lambda r: _parse_ts(r["updated_at"], "updated_at")) if candidates else None


def parse_recovery(payload) -> tuple[list[Reading], list[str]]:
    """RecoveryCollection -> ([whoop_recovery reading] or [], notes explaining any absence)."""
    records = _records(payload)
    for r in records:
        for key in ("score_state", "updated_at", "cycle_id"):
            if key not in r:
                raise SchemaDrift(SOURCE, f"recovery record lacks {key}")
    newest = max(records, key=lambda r: _parse_ts(r["updated_at"], "updated_at"), default=None)
    if newest is None:
        return [], ["no recovery records returned"]
    if newest["score_state"] != "SCORED":
        return [], [f"latest recovery is {newest['score_state']}: not scored yet, so no readiness signal"]
    score = newest.get("score") or {}
    if "recovery_score" not in score:
        raise SchemaDrift(SOURCE, "SCORED recovery without score.recovery_score")
    if score.get("user_calibrating"):
        return [], ["WHOOP is still calibrating this user: recovery score not usable"]
    value = score["recovery_score"]
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= 100:
        raise SchemaDrift(SOURCE, f"recovery_score out of range: {value!r}")
    return [Reading("whoop_recovery", float(value), SOURCE, _parse_ts(newest["updated_at"], "updated_at"))], []


def parse_sleep(payload) -> tuple[list[Reading], list[str]]:
    """PaginatedSleepResponse -> ([sleep_hours reading] or [], notes). Naps are ignored."""
    records = _records(payload)
    for r in records:
        for key in ("score_state", "updated_at", "nap"):
            if key not in r:
                raise SchemaDrift(SOURCE, f"sleep record lacks {key}")
    newest = _newest(records, lambda r: not r["nap"])
    if newest is None:
        return [], ["no non-nap sleep records returned"]
    if newest["score_state"] != "SCORED":
        return [], [f"latest sleep is {newest['score_state']}: not scored yet"]
    stages = (newest.get("score") or {}).get("stage_summary")
    if not isinstance(stages, dict):
        raise SchemaDrift(SOURCE, "SCORED sleep without score.stage_summary")
    try:
        asleep_ms = sum(stages[k] for k in ("total_light_sleep_time_milli", "total_slow_wave_sleep_time_milli",
                                           "total_rem_sleep_time_milli"))
    except KeyError as err:
        raise SchemaDrift(SOURCE, f"stage_summary lacks {err.args[0]}") from err
    hours = asleep_ms / MS_PER_HOUR
    if not 0 <= hours <= 16:
        raise SchemaDrift(SOURCE, f"implausible sleep duration: {hours:.1f} h")
    return [Reading("sleep_hours", round(hours, 2), SOURCE, _parse_ts(newest["updated_at"], "updated_at"))], []


class TokenStore:
    """Mode-0600 token file + OS file lock so only one process refreshes at a time."""

    def __init__(self, path):
        self.path = pathlib.Path(path)
        self.lock_path = self.path.with_name(self.path.name + ".lock")

    def load(self) -> dict | None:
        return localsecrets.read_json_private(self.path)

    def save(self, tokens: dict) -> None:
        for key in ("access_token", "refresh_token"):
            if not tokens.get(key):
                raise SourceDown(SOURCE, f"refresh response lacked {key}")
        localsecrets.write_json_private(self.path, tokens)

    def refresh_if_still_stale(self, seen_refresh_token: str, refresh_fn) -> dict:
        """Refresh at most once across all processes.

        `seen_refresh_token` is the refresh token the caller held when its access token was rejected. Under the
        lock we reload: if the stored refresh token has changed, another refresher already rotated, so reuse its
        tokens; otherwise call refresh_fn(seen_refresh_token) and persist the NEW pair atomically. If refresh_fn
        raises, nothing is written (the old pair stays on disk for diagnosis).
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            current = self.load()
            if current and current.get("refresh_token") != seen_refresh_token:
                return current
            try:
                fresh = refresh_fn(seen_refresh_token)
            except SourceDown:
                raise
            except Exception as err:  # noqa: BLE001 - never leak token material in the message
                raise SourceAuthError(SOURCE, f"token refresh failed: {type(err).__name__}") from err
            self.save(fresh)
            return fresh
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
