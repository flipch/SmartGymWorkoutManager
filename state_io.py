"""Local state files shared by the web app and the MCP server (readiness, coach feedback).

Two processes read-modify-write the same JSON files, so: writes are atomic (temp file + fsync + rename), appends hold an
flock, a corrupt file is moved aside instead of being silently overwritten, and every value is validated on the way in.
Validation errors name the FIELD only, never echo the value.
"""
import contextlib
import fcntl
import json
import math
import os
import re
import tempfile
import time
from datetime import datetime, timezone

READINESS_MAX_AGE_H = 36  # a recovery reading older than this does not describe today
MAX_NOTE = 500
MAX_NAME = 120
_CODE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def utc_stamp(now=None) -> str:
    """Sortable UTC timestamp with a fixed width (string comparison == time comparison)."""
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_stamp(text):
    """The datetime for a stamp written by utc_stamp or the older isoformat()+'Z'; None when unreadable."""
    if not isinstance(text, str):
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def atomic_write_json(path, data) -> None:
    path = os.fspath(path)
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=os.path.basename(path) + ".")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o644)  # mkstemp makes 0600; these files are read by the sibling MCP container
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def file_lock(path, timeout: float = 10.0):
    """Exclusive advisory lock across processes; raises TimeoutError instead of waiting forever."""
    os.makedirs(os.path.dirname(os.fspath(path)) or ".", exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("state file is locked") from None
                time.sleep(0.02)
        yield
    finally:
        os.close(fd)  # closing releases the flock


def quarantine(path) -> str:
    """Move an unreadable state file aside (never delete evidence) and return the new name."""
    base = f"{path}.corrupt-{int(time.time())}"
    target, n = base, 0
    while os.path.exists(target):  # two damaged files in one second must not overwrite each other
        n += 1
        target = f"{base}-{n}"
    os.replace(path, target)
    return target


# ------------------------------------------------------------------------------------------------ validation
def _number(payload, key, low, high):
    value = payload[key]
    try:
        finite = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:  # an integer too large for a float is not a usable number either
        finite = False
    if not finite:
        raise ValueError(f"invalid {key}: must be a number")
    if not low <= value <= high:
        raise ValueError(f"invalid {key}: must be between {low} and {high}")
    return value


def _text(payload, key, limit, *, pattern=None):
    value = payload[key]
    if not isinstance(value, str):
        raise ValueError(f"invalid {key}: must be text")
    value = "".join(ch for ch in value if ch >= " " or ch in "\n\t").strip()
    if not value:
        raise ValueError(f"invalid {key}: empty")
    if len(value) > limit:
        raise ValueError(f"invalid {key}: longer than {limit} characters")
    if pattern is not None and not pattern.fullmatch(value):
        raise ValueError(f"invalid {key}: unexpected characters")
    return value


# every field a stored readiness can carry; a reading with ANY of them must reach the program/autoregulation
READINESS_SIGNALS = ("whoop_recovery", "hrv_vs_baseline", "rhr_delta_bpm", "sleep_hours", "subjective", "acwr")


def validate_readiness(payload) -> dict:
    """Known keys only, each range-checked. {} means 'nothing usable was supplied'."""
    if not isinstance(payload, dict):
        raise ValueError("readiness must be a JSON object")
    out = {}
    if "whoop_recovery" in payload:
        out["whoop_recovery"] = _number(payload, "whoop_recovery", 0, 100)
    if "hrv_vs_baseline" in payload:
        value = payload["hrv_vs_baseline"]
        if not isinstance(value, str) or value.lower() not in ("above", "within", "below"):
            raise ValueError("invalid hrv_vs_baseline: must be above, within or below")
        out["hrv_vs_baseline"] = value.lower()
    if "rhr_delta_bpm" in payload:
        out["rhr_delta_bpm"] = _number(payload, "rhr_delta_bpm", -30, 40)
    if "sleep_hours" in payload:
        out["sleep_hours"] = _number(payload, "sleep_hours", 0, 24)
    if "subjective" in payload:
        out["subjective"] = _number(payload, "subjective", 1, 5)
    if "acwr" in payload:
        out["acwr"] = _number(payload, "acwr", 0, 5)
    return out


def require_readiness(payload) -> dict:
    """validate_readiness for a reading the CALLER supplied: a typo or an empty object is an error, because falling back
    to "no reading" would plan a full-volume session on top of a red stored one."""
    if isinstance(payload, dict):
        unknown = sorted(str(k)[:40] for k in payload if k not in READINESS_SIGNALS)
        if unknown:
            raise ValueError(f"unknown readiness field(s): {', '.join(unknown)}; use {', '.join(READINESS_SIGNALS)}")
    out = validate_readiness(payload)
    if not out:
        raise ValueError(f"readiness has no signal; give at least one of {', '.join(READINESS_SIGNALS)}")
    return out


# Where a stored signal came from. A small closed set: a new source is one more entry here and in READINESS_SOURCE_FIELDS.
READINESS_SOURCES = ("whoop", "manual")
# what each source can actually supply (WHOOP gives no HRV/resting-HR baseline, no subjective score, no ACWR)
READINESS_SOURCE_FIELDS = {"whoop": ("whoop_recovery", "sleep_hours"),
                           "manual": READINESS_SIGNALS}


def split_source(payload):
    """(payload without `source`, source or None) for a reading being SAVED. A source that is not one of READINESS_SOURCES,
    or that names a field its source cannot supply, is an error: provenance must be as trustworthy as the value."""
    if not isinstance(payload, dict) or "source" not in payload:
        return payload, None
    rest = {k: v for k, v in payload.items() if k != "source"}
    source = payload["source"]
    if not isinstance(source, str) or source not in READINESS_SOURCES:
        raise ValueError(f"invalid source: must be one of {', '.join(READINESS_SOURCES)}")
    bad = sorted(k for k in rest if k in READINESS_SIGNALS and k not in READINESS_SOURCE_FIELDS[source])
    if bad:
        raise ValueError(f"invalid source: {source} cannot supply {', '.join(bad)}")
    return rest, source


def clean_sources(stored_sources, keys):
    """The per-field sources of a stored reading, trusted only for known fields and known sources (the file can be hand-edited)."""
    if not isinstance(stored_sources, dict):
        return {}
    return {k: v for k, v in stored_sources.items()
            if k in keys and isinstance(v, str) and v in READINESS_SOURCES and k in READINESS_SOURCE_FIELDS[v]}


# ------------------------------------------------------------------------------------------------ coach preferences
PREF_GOALS = ("muscle", "strength", "fatloss", "general")  # keep equal to coach.GOALS (a test pins it)
PREF_EXPERIENCE = ("beginner", "intermediate", "advanced")
PREF_FIELDS = ("goal", "days_per_week", "experience", "one_rm", "load_unit")
MAX_ONE_RM_ENTRIES = 30
MAX_ONE_RM = 1000  # kg or lb: a 1RM above this is a typo


def validate_preferences(payload) -> dict:
    """The coach page's remembered choices. Unknown fields, bad values and junk 1RMs are errors, never silently dropped
    (a typo'd goal would otherwise become a different program). Every field is optional; an empty object is valid (= nothing remembered)."""
    if not isinstance(payload, dict):
        raise ValueError("preferences must be a JSON object")
    unknown = sorted(str(k)[:40] for k in payload if k not in PREF_FIELDS)
    if unknown:
        raise ValueError(f"unknown preference field(s): {', '.join(unknown)}; use {', '.join(PREF_FIELDS)}")
    out = {}
    if "goal" in payload:
        if not isinstance(payload["goal"], str) or payload["goal"] not in PREF_GOALS:
            raise ValueError(f"invalid goal: must be one of {', '.join(PREF_GOALS)}")
        out["goal"] = payload["goal"]
    if "experience" in payload:
        if not isinstance(payload["experience"], str) or payload["experience"] not in PREF_EXPERIENCE:
            raise ValueError(f"invalid experience: must be one of {', '.join(PREF_EXPERIENCE)}")
        out["experience"] = payload["experience"]
    if "days_per_week" in payload:
        d = payload["days_per_week"]
        if isinstance(d, bool) or not isinstance(d, int) or not 1 <= d <= 5:
            raise ValueError("invalid days_per_week: must be a whole number from 1 to 5")
        out["days_per_week"] = d
    if "load_unit" in payload:
        if payload["load_unit"] not in ("kg", "lb"):
            raise ValueError("invalid load_unit: must be kg or lb")
        out["load_unit"] = payload["load_unit"]
    if "one_rm" in payload:
        rms = payload["one_rm"]
        if not isinstance(rms, dict):
            raise ValueError("invalid one_rm: must be an object of exercise or muscle name to a number")
        if len(rms) > MAX_ONE_RM_ENTRIES:
            raise ValueError(f"invalid one_rm: at most {MAX_ONE_RM_ENTRIES} entries")
        clean = {}
        for name, value in rms.items():
            key = _text({"name": name}, "name", MAX_NAME) if isinstance(name, str) else None
            if key is None or not re.search(r"[^\W_]", key):
                raise ValueError("invalid one_rm: each name must be text with letters or digits")
            try:
                clean[key] = _number({"v": value}, "v", 0.1, MAX_ONE_RM)
            except ValueError:
                raise ValueError(f"invalid one_rm: each value must be a number from 0.1 to {MAX_ONE_RM}") from None
        if clean and out.get("load_unit") is None:
            raise ValueError("invalid load_unit: say kg or lb when you give 1RMs")
        out["one_rm"] = clean
    return out


DIFFICULTY = ("too_easy", "ok", "too_hard")


FEEDBACK_FIELDS = ("rpe", "difficulty", "completion", "soreness", "pain", "exercise", "avoid", "prefer", "note", "workout_code")


def validate_feedback(payload) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("feedback must be a JSON object")
    unknown = sorted(str(k)[:40] for k in payload if k not in FEEDBACK_FIELDS)
    if unknown:  # a typo'd "Pain" would otherwise be logged as a session without the pain
        raise ValueError(f"unknown feedback field(s): {', '.join(unknown)}; use {', '.join(FEEDBACK_FIELDS)}")
    out = {}
    if "rpe" in payload:
        out["rpe"] = _number(payload, "rpe", 1, 10)
    if "difficulty" in payload:
        if payload["difficulty"] not in DIFFICULTY:
            raise ValueError("invalid difficulty: must be too_easy, ok or too_hard")
        out["difficulty"] = payload["difficulty"]
    if "completion" in payload:
        out["completion"] = _text(payload, "completion", 40)
    if "soreness" in payload:
        out["soreness"] = _number(payload, "soreness", 1, 5)
    if "pain" in payload:
        if not isinstance(payload["pain"], bool):
            raise ValueError("invalid pain: must be true or false")
        out["pain"] = payload["pain"]
    for key in ("avoid", "exercise"):
        if key in payload:
            out[key] = _text(payload, key, MAX_NAME)
            if not re.search(r"[^\W_]", out[key]):  # '???' or an emoji matches no exercise: it would look protected and protect nothing
                raise ValueError(f"invalid {key}: name an exercise using letters or digits")
    if out.get("pain") and not (out.get("exercise") or out.get("avoid")):
        # a pain report only protects you through the exercise it names: accepting one without it would do nothing
        raise ValueError("pain: say which exercise hurt (exercise or avoid)")
    if "prefer" in payload:
        out["prefer"] = _text(payload, "prefer", MAX_NAME)
    if "note" in payload:
        out["note"] = _text(payload, "note", MAX_NOTE)
    if "workout_code" in payload:
        out["workout_code"] = _text(payload, "workout_code", 64, pattern=_CODE)
    return out
