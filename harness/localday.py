"""One definition of "which day is it": the user's local calendar day.

Vendors disagree (WHOOP cycles, UTC timestamps, device-local dates). Everything crossing a port
boundary is converted here, once, so an evening lift cannot land on tomorrow.
"""
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

DEFAULT_TZ = "America/Los_Angeles"


def to_seconds(ts: float) -> float:
    """Vendors mix epoch seconds and milliseconds; anything past year ~5138 in seconds is ms."""
    return ts / 1000.0 if ts > 1e11 else float(ts)


def local_day(ts: float, tz: str = DEFAULT_TZ) -> date:
    """Local calendar date of an epoch timestamp (seconds or milliseconds)."""
    return datetime.fromtimestamp(to_seconds(ts), tz=timezone.utc).astimezone(ZoneInfo(tz)).date()


def today(tz: str = DEFAULT_TZ, now: datetime | None = None) -> date:
    """Today's local date. `now` (tz-aware) is injectable for tests."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(ZoneInfo(tz)).date()
