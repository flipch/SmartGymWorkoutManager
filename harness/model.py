"""Vendor-neutral domain types. Nothing here knows about Speediance, WHOOP, or any API shape."""
from dataclasses import dataclass, field
from datetime import date, datetime

# Readiness signal kinds understood by the planner (keys match coach.autoregulate()).
READINESS_KINDS = ("whoop_recovery", "hrv_vs_baseline", "rhr_delta_bpm", "sleep_hours", "subjective", "acwr")


@dataclass(frozen=True)
class SetResult:
    exercise: str
    reps: int | None = None
    load: float | None = None
    seconds: float | None = None
    unit: str | None = None  # "kg" | "lb" | None (unknown: never guess)
    target: int | None = None  # the rep target the template asked for (None/0 = none recorded)
    side: str | None = None  # "left" | "right" for a one-sided set, None for bilateral


@dataclass(frozen=True)
class SessionResult:
    source_id: str
    local_day: date
    started_at_ms: int
    title: str
    duration_s: int | None = None
    total_volume: float | None = None
    sets: tuple[SetResult, ...] = ()


@dataclass(frozen=True)
class Reading:
    """One readiness signal with provenance, so staleness can be checked."""
    kind: str
    value: float | str
    source: str
    as_of: datetime  # tz-aware: when the source produced it


@dataclass(frozen=True)
class Unavailable:
    """The planner refuses to advise. Always lists why, so the human knows what to fix."""
    reasons: tuple[str, ...]

    def __bool__(self) -> bool:  # an Unavailable is falsy: `if plan:` can never treat it as a plan
        return False


@dataclass(frozen=True)
class ProposedExercise:
    name: str
    sets: int
    reps: int | None  # None = no recorded rep target; use your template's
    rir: int
    mode: str
    load: float | None  # None = no logged history to anchor on; choose by RIR
    load_basis: str  # human-readable reason for the load (or the lack of one)
    last_load: float | None = None


@dataclass(frozen=True)
class Plan:
    local_day: date
    band: str  # low | moderate | high
    session: str  # push | as_planned | reduce_or_recover
    message: str
    signals: tuple[str, ...]
    overrides: tuple[str, ...]
    day_name: str
    exercises: tuple[ProposedExercise, ...]
    inputs: dict = field(default_factory=dict)  # provenance: which feeds, as-of times, session count
    warnings: tuple[str, ...] = ()
    template_title: str | None = None  # the user's own template this was built from, if any
    source: str = "coach_catalog"  # "user_template" | "coach_catalog"
