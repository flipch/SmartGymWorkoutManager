"""Speediance payloads -> vendor-neutral SessionResult. Pure functions; no I/O.

Shapes verified against live recordings on 2026-10-26: a history record is a flat dict; a detail payload is a
list of exercises, each with `finishedReps` (the sets). Per-rep arrays live in `trainingInfoDetail`.
"""
from statistics import median

from .errors import SchemaDrift
from .localday import DEFAULT_TZ, local_day
import math

from .model import SessionResult, SetResult

SOURCE = "speediance"


def _first_nonzero_weights(set_row: dict) -> list:
    """The per-rep load array for a set. `weights` is the machine's own load figure: on real one-sided sets it equals
    capacity/reps (74 on a Bulgarian split squat) while leftWeights/rightWeights hold half of it (37), and bilateral
    sets use it too, so it is the only figure that is comparable across sets of the same lift. The trained side's array
    is only a fallback for a set that has no `weights`."""
    info = set_row.get("trainingInfoDetail") or {}
    side = {1: "leftWeights", 2: "rightWeights"}.get(set_row.get("leftRight"))
    for key in ("weights", side, "leftWeights", "rightWeights"):
        arr = info.get(key) if key else None
        if isinstance(arr, list) and any(isinstance(w, (int, float)) and w > 0 for w in arr):
            return [w for w in arr if isinstance(w, (int, float))]
    return []


def _count(value):
    """A rep count or duration the machine could have produced: finite and not negative, else None (never evidence)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return value


def _whole_count(value):
    """A rep count: a whole number of reps. 0.1 reps is not a set, and capacity / 0.1 would be a 10x load."""
    n = _count(value)
    return n if n is not None and n == int(n) else None


def set_load(set_row: dict) -> float | None:
    """Representative working load for one set: median of positive per-rep loads (robust to
    chains/eccentric mid-set changes); else capacity/reps; else None. Unit is NOT inferred here."""
    weights = [w for w in _first_nonzero_weights(set_row) if w > 0 and math.isfinite(w)]
    if weights:
        mid = float(median(weights))
        if min(weights) < 0.5 * mid:  # a drop set: no single load was lifted for the counted reps, so none is an anchor
            return None
        if sum(1 for w in weights if w < 0.9 * mid) > 0.25 * len(weights):  # a two-level set (100 then 75) has a median nobody lifted
            return float(sorted(weights)[(len(weights) - 1) // 4])  # what at least three quarters of the reps carried
        return float(mid)
    reps, capacity = _whole_count(set_row.get("finishedCount")) or 0, set_row.get("capacity") or 0
    if isinstance(reps, (int, float)) and isinstance(capacity, (int, float)) and reps > 0 and capacity > 0 and math.isfinite(capacity):
        return round(capacity / reps, 2)
    return None


def session_header(record: dict, tz: str = DEFAULT_TZ) -> SessionResult:
    """History-list record -> SessionResult without sets (list rows carry only per-body-part counts)."""
    for key in ("trainingId", "startTimestamp"):
        if record.get(key) is None:
            raise SchemaDrift(SOURCE, f"history record lacks {key}")
    return SessionResult(
        source_id=str(record["trainingId"]),
        local_day=local_day(record["startTimestamp"], tz),
        started_at_ms=int(record["startTimestamp"] * (1000 if record["startTimestamp"] < 1e11 else 1)),
        title=str(record.get("title") or "Workout"),
        duration_s=record.get("trainingTime"),
        total_volume=record.get("totalCapacity"),
    )


def session_with_sets(record: dict, detail, tz: str = DEFAULT_TZ, unit: str | None = None) -> SessionResult:
    """Detail payload -> SessionResult with one SetResult per finished set."""
    if not isinstance(detail, list):
        raise SchemaDrift(SOURCE, "detail payload was not a list of exercises")
    sets = []
    for ex in detail:
        if not isinstance(ex, dict) or "actionLibraryName" not in ex:
            raise SchemaDrift(SOURCE, "exercise lacks actionLibraryName")
        name = str(ex["actionLibraryName"])
        for row in ex.get("finishedReps") or []:
            if not isinstance(row, dict):
                raise SchemaDrift(SOURCE, "set row was not an object")
            timed = ex.get("completionMethod") in (0, 2)  # timer-based exercise (2 = Row/Ski timed in the editor): reps are not meaningful
            reps = None if timed else (_whole_count(row.get("finishedCount")) or None)
            sets.append(SetResult(exercise=name, reps=reps, load=set_load(row),
                                  seconds=_count(row.get("time")) if timed else None, unit=unit,
                                  target=None if timed else (_whole_count(row.get("targetCount")) or None),
                                  side={1: "left", 2: "right"}.get(row.get("leftRight")) or ("unknown" if ex.get("isLeftRight") == 1 else None)))  # a duration is not a rep target
    header = session_header(record, tz)
    return SessionResult(header.source_id, header.local_day, header.started_at_ms, header.title,
                         header.duration_s, header.total_volume, tuple(sets))
