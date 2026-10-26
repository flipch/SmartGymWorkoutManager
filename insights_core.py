"""Pure training-frequency maths shared by the web UI (features.py) and the MCP server (mcp_server.py).

The two used to carry copies of this logic with the same bugs: windows that were one day too wide (`>= today - 7`
counted 8 calendar days), future-dated records leaking into "days since last", and an empty/failed history reading as
"well rested". Everything here takes `dates` and `today` explicitly, so it is testable without a clock or a network.
"""
import re
from datetime import date, timedelta

MAX_DAYS = 365
MAX_N = 50


def clamp_days(value, default: int = 30) -> int:
    """A day-count argument bounded to 1..MAX_DAYS. Non-numbers and bools fall back to `default`."""
    if isinstance(value, bool):
        return default
    try:
        n = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(1, min(n, MAX_DAYS))


def clamp_n(value, default: int = 1) -> int:
    if isinstance(value, bool):
        return default
    try:
        n = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(1, min(n, MAX_N))


def _usable(dates, today: date) -> list[date]:
    """Sorted distinct days that are not in the future (a future-dated record is a data problem, not training)."""
    return sorted({d for d in dates if isinstance(d, date) and d <= today})


def in_last(dates, today: date, n_days: int) -> int:
    """Sessions on the last `n_days` calendar days INCLUDING today (so 7 means today-6 .. today)."""
    return sum(1 for d in _usable(dates, today) if today - timedelta(days=n_days - 1) <= d)


def summarize(dates, today: date) -> dict:
    days = _usable(dates, today)
    have = set(days)
    streak = 0
    cur = today if today in have else today - timedelta(days=1)
    while cur in have:
        streak += 1
        cur -= timedelta(days=1)
    last30 = in_last(days, today, 30)
    return {
        "current_streak_days": streak,
        "sessions_last_7d": in_last(days, today, 7),
        "sessions_last_30d": last30,
        "avg_sessions_per_week": round(last30 / (30 / 7), 1) if last30 else 0,
        "last_session": days[-1].isoformat() if days else None,
        "active_days": len(days),
    }


def recovery_advice(dates, today: date) -> dict:
    days = _usable(dates, today)
    last7 = in_last(days, today, 7)
    days_since = (today - days[-1]).days if days else None
    if days_since is None or days_since >= 3:
        rec, msg = "train", "Well rested — a great day to train. 💪"
    elif last7 >= 6:
        rec, msg = "rest", "6+ sessions in 7 days. Take a rest or active-recovery day. 🛌"
    elif days_since == 0 and last7 >= 2:
        rec, msg = "active_recovery", "Already trained today — keep it light if anything. 🚶"
    else:
        rec, msg = "train", "Good to train today. ✅"
    return {"recommendation": rec, "message": msg, "days_since_last": days_since, "sessions_last_7d": last7}


_RECORD_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def valid_record_id(value) -> bool:
    """A vendor record/training id used inside an upstream URL path: no slashes, dots, queries or whitespace."""
    return isinstance(value, str) and _RECORD_ID.fullmatch(value) is not None


def json_object(value):
    """A request body is only usable as a dict; a JSON list, string or number is "no body" (never an AttributeError)."""
    return value if isinstance(value, dict) else {}


def valid_iso_date(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return len(value) == 10
