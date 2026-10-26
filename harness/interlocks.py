"""Pure safety validators for proposed workouts and input freshness. No I/O, no push path.

Rules (from the pre-mortem / skeptic review, enforced server-side rather than in a prompt):
  * a proposed load must be anchored to a logged load for that exercise (never invented);
  * a load may exceed the last logged load by at most `max_jump_pct`;
  * only allowed machine modes (default: Standard; Eccentric/Chains stay off until done by hand);
  * optional absolute cap and optional known-exercise whitelist (both need user-supplied facts, so default off);
  * readiness inputs older than `max_age_h` do not count.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable

from .model import ProposedExercise, Reading

DEFAULT_MAX_JUMP_PCT = 10.0
DEFAULT_MAX_READING_AGE_H = 18.0
ALLOWED_MODES = frozenset({"standard", "as_template"})  # as_template = whatever the user's own saved template uses


@dataclass(frozen=True)
class Violation:
    code: str  # load_jump | unanchored_load | mode_not_allowed | unknown_exercise | abs_load | bad_prescription
    exercise: str
    detail: str


def clamp_to_jump(proposed: float, last: float, max_jump_pct: float = DEFAULT_MAX_JUMP_PCT) -> float:
    """Largest load we may propose given the last logged load."""
    return min(proposed, last * (1 + max_jump_pct / 100.0))


def validate_exercises(exercises: Iterable[ProposedExercise], *, max_jump_pct: float = DEFAULT_MAX_JUMP_PCT,
                       allowed_modes=ALLOWED_MODES, known_exercises: set[str] | None = None,
                       max_abs_load: float | None = None) -> list[Violation]:
    out: list[Violation] = []
    for ex in exercises:
        if ex.sets < 1 or (ex.reps is not None and ex.reps < 1):
            out.append(Violation("bad_prescription", ex.name, f"sets={ex.sets} reps={ex.reps}"))
        if ex.mode not in allowed_modes:
            out.append(Violation("mode_not_allowed", ex.name, f"mode {ex.mode!r} not in {sorted(allowed_modes)}"))
        if known_exercises is not None and ex.name not in known_exercises:
            out.append(Violation("unknown_exercise", ex.name, "not in the user's exercise library"))
        if ex.load is not None:
            if ex.last_load is None:
                out.append(Violation("unanchored_load", ex.name, "load proposed without a logged load to anchor it"))
            elif ex.load > ex.last_load * (1 + max_jump_pct / 100.0) + 1e-9:
                out.append(Violation("load_jump", ex.name,
                                     f"{ex.load:g} exceeds last logged {ex.last_load:g} by more than {max_jump_pct:g}%"))
            if max_abs_load is not None and ex.load > max_abs_load:
                out.append(Violation("abs_load", ex.name, f"{ex.load:g} exceeds absolute cap {max_abs_load:g}"))
    return out


def split_fresh(readings: Iterable[Reading], now: datetime,
                max_age_h: float = DEFAULT_MAX_READING_AGE_H) -> tuple[list[Reading], list[Reading]]:
    """(fresh, stale). A reading from the future (clock skew > 5 min) counts as stale: do not trust it."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    fresh, stale = [], []
    for r in readings:
        age = now - r.as_of
        (fresh if timedelta(minutes=-5) <= age <= timedelta(hours=max_age_h) else stale).append(r)
    return fresh, stale
