"""today's plan: freshness gate -> autoregulation band -> next template -> anchored loads -> interlocks.

Pure: takes already-fetched data, returns a Plan or Unavailable. It never fetches, never writes, never pushes.
Design rules enforced here:
  * a failed or missing feed yields Unavailable, never a default "train" recommendation
    (coach.autoregulate() alone defaults to MODERATE when it has no signals, so we gate BEFORE calling it);
  * the proposal is the user's OWN next template, progressed from their last logged sets. Only when no usable
    template history exists do we fall back to the generic coach catalog (whose names rarely match real exercises);
  * loads are relative to logged loads only; with no logged anchor the exercise gets no load ("choose by RIR");
  * progression is conservative (coach: "moderate = hold intensity, no PRs"): raise only on a HIGH-readiness day
    when every logged set hit its rep target; a low-readiness day reduces load and sets;
  * everything is validated by harness.interlocks before it is returned.
"""
import re
from collections import Counter
from datetime import date, datetime, timedelta
from statistics import median, median_high, median_low

import coach

from . import interlocks
from .localday import DEFAULT_TZ, today as local_today
from .model import READINESS_KINDS, Plan, ProposedExercise, Reading, SessionResult, SetResult, Unavailable

PROGRESSION_PCT = 2.5
ROUND_TO = 0.5
TEMPLATE_WINDOW_DAYS = 90
TEMPLATE_MIN_COUNT = 2
ANCHOR_MAX_AGE_DAYS = 90  # a load logged longer ago than this is not trusted as an anchor
LAYOFF_DAYS = 14          # after this long without logging a lift, hold its load: no increases




def _mid(values: list[int]) -> float | int:
    """True median of rep counts (the average of the two middle values for an even count), shown without a trailing .0."""
    m = median(values)
    return int(m) if m == int(m) else m

def _norm(name: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", name.lower()).split())


# A parenthetical that only says WHERE on the machine a lift is done is not part of the movement's identity.
LOCATION_NOTE = re.compile(r"\(\s*main\s+frame\s*\)", re.I)


def _fold(word: str) -> str:
    """Fold simple plurals so 'Presses'/'Press', 'Abs'/'Ab', 'Dips'/'Dip' compare equal (both sides use this)."""
    if word.endswith("sses") and len(word) > 5:
        return word[:-2]
    if len(word) > 2 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _tokens(name: str) -> frozenset[str]:
    """Normalised word set: lowercase, punctuation removed (parenthetical words are KEPT: '(Seated)' vs '(Standing)'
    is a different movement), plurals folded."""
    name = LOCATION_NOTE.sub(" ", name)
    return frozenset(_fold(w) for w in re.sub(r"[^a-z0-9 ]", " ", name.lower()).split())


def _same_exercise(a: str, b: str) -> bool:
    """Same movement iff the word sets are equal. Deliberately NOT fuzzy: one differing word (incline/decline,
    biceps/triceps, abduction/adduction, left/right) is a different exercise, and a wrong anchor is worse than none."""
    ta, tb = _tokens(a), _tokens(b)
    return bool(ta) and ta == tb


def _round(x: float, step: float = ROUND_TO) -> float:
    return round(x / step) * step


def _grid(sets) -> float:
    """The machine's load step for these sets: whole pounds, half kilograms."""
    return 1.0 if any(getattr(s, "unit", None) == "lb" for s in sets) else ROUND_TO


# ── template selection ──────────────────────────────────────────────────────
def next_template_title(sessions: list[SessionResult], day: date, *, override: str | None = None,
                        window_days: int = TEMPLATE_WINDOW_DAYS, min_count: int = TEMPLATE_MIN_COUNT) -> str | None:
    """The user's recurring workout that has gone longest without being done (rotation heuristic).

    Recurring = done >= min_count times in the window. One-offs ("quick leg shoulder") never qualify.
    Titles already logged today are skipped. `override` picks the newest title containing that text.
    """
    if override:
        hits = [s for s in sessions if override.lower() in s.title.lower()]
        return max(hits, key=lambda s: s.started_at_ms).title.strip() if hits else None
    done_today = {s.title.strip() for s in sessions if s.local_day == day}
    recent = [s for s in sessions if day - timedelta(days=window_days) <= s.local_day < day]
    counts = Counter(s.title.strip() for s in recent)
    last_done: dict[str, date] = {}
    for s in recent:
        t = s.title.strip()
        last_done[t] = max(last_done.get(t, s.local_day), s.local_day)
    candidates = [t for t, c in counts.items() if c >= min_count and t not in done_today]
    return min(candidates, key=lambda t: (last_done[t], t)) if candidates else None


def newest_with_title(sessions: list[SessionResult], title: str) -> SessionResult | None:
    hits = [s for s in sessions if s.title.strip() == title.strip()]
    return max(hits, key=lambda s: s.started_at_ms) if hits else None


# ── helpers over logged sets ────────────────────────────────────────────────
def side_missing(sets) -> bool:
    """A one-sided exercise where one side logged no completed set."""
    sides = {s.side for s in sets if s.side}
    done = {s.side for s in sets if s.side and s.load and (s.reps or getattr(s, "seconds", None))}
    return bool(sides) and done != {"left", "right"}  # logging only the left side is a half-done lift too


def _lifted_median(loads) -> float:
    """The median load, but never a load nobody lifted: when the two middle loads are far apart (an even ramp, 10 lb and
    100 lb) the LOWER one stands, not their average."""
    lo, hi = median_low(loads), median_high(loads)
    return float(median(loads)) if hi <= lo * 1.25 else float(lo)


def _working_reps(sets) -> list[int]:
    """Sorted rep counts of the sets at the working load (a ramp's 1-rep top set is not the rep count of the lift)."""
    done = [x for x in sets if x.reps]
    loads = [x.load for x in done if x.load]
    mid = _lifted_median(loads) if loads else None
    near = [x.reps for x in done if mid and x.load and abs(x.load - mid) <= 0.1 * mid]
    return sorted(near or (x.reps for x in done))


def anchor_load(sets) -> float | None:
    """The load a progression is built on. One-sided sets keep their side: the WEAKER side's median is the anchor, so an
    asymmetric 20/40 history never prescribes a load the weaker side has not done (averaging would give 30)."""
    counted = [s for s in sets if s.load and (s.reps or getattr(s, "seconds", None))]
    if side_missing(sets):  # a side that never completed a set has no anchor: the other side's load must not stand in for it
        return None
    by_side: dict = {}
    for s in counted:
        by_side.setdefault(s.side, []).append(s.load)
    if not by_side:
        return None
    medians = [_lifted_median(v) for v in by_side.values()]
    return min(medians)


def asymmetry_note(sets) -> str | None:
    by_side = {}
    for s in sets:
        if s.load and s.side:
            by_side.setdefault(s.side, []).append(s.load)
    if len(by_side) < 2 or not {"left", "right"} <= set(by_side):
        return None
    left, right = (float(median(by_side[k])) for k in ("left", "right") if k in by_side)
    if abs(left - right) <= 1e-9:
        return None
    return f"left {left:g} vs right {right:g}: anchored on the lower side"


def _performed(s) -> bool:
    return bool(s.load or s.reps or getattr(s, "seconds", None))


def _newest_logged(name: str, sessions: list[SessionResult], since: date | None = None):
    """(sets, day) of the newest session that logged this exercise with a load, whatever its quality."""
    for s in sorted(sessions, key=lambda x: x.started_at_ms, reverse=True):
        if since is not None and s.local_day < since:
            continue
        sets = [x for x in s.sets if _same_exercise(x.exercise, name)]
        if any(_performed(x) for x in sets):  # a session whose load was not usable (a drop set) is still the latest word
            return sets, s.local_day
    return None


def last_logged(name: str, sessions: list[SessionResult], *, since: date | None = None):
    """(median load, sets, day) from the newest session that logged this exercise with loads (catalog fallback).

    Sessions before `since` are ignored: an old load is not an anchor.
    """
    for s in sorted(sessions, key=lambda x: x.started_at_ms, reverse=True):
        if since is not None and s.local_day < since:
            continue
        sets = [x for x in s.sets if _same_exercise(x.exercise, name)]
        if any(_performed(x) for x in sets):
            anchor = anchor_load(sets)
            if anchor is None:
                return None  # loads but no completed set (or a missing side): the latest word is "not done", not an older session's success
            return anchor, sets, s.local_day
    return None


def _by_exercise(sets: tuple[SetResult, ...]) -> dict[str, list[SetResult]]:
    out: dict[str, list[SetResult]] = {}
    for s in sets:
        out.setdefault(s.exercise, []).append(s)
    return out


def _readiness_dict(fresh: list[Reading]) -> tuple[dict, list[str]]:
    latest: dict[str, Reading] = {}
    for r in sorted(fresh, key=lambda r: r.as_of):
        latest[r.kind] = r
    data = {k: r.value for k, r in latest.items() if k in READINESS_KINDS}
    prov = [f"{k}={r.value} ({r.source}, {r.as_of.isoformat(timespec='minutes')})" for k, r in sorted(latest.items())]
    return data, prov


def _progress(last: float, hit_all: bool, band: str, intensity_pct: float, max_jump_pct: float,
              *, allow_increase: bool = True, step: float = ROUND_TO):
    """(load, basis) for one exercise given its last logged load.

    Never exceeds the jump cap, and never exceeds `last` unless it is a deliberate progression. Rounding to the
    plate grid only ever steps DOWN past a ceiling, so a "hold" can come out up to one step below the logged load.
    """
    if band == "high" and hit_all and not allow_increase:
        proposed, basis = last, f"hold the last logged load (no increases after {LAYOFF_DAYS}+ days without this lift)"
    elif band == "low":
        proposed, basis = last * (1 + intensity_pct / 100.0), "low readiness: load reduced from last logged"
    elif band == "high" and hit_all:
        proposed, basis = last * (1 + PROGRESSION_PCT / 100.0), "high readiness and every last set hit its rep target: small increase"
    elif hit_all:
        proposed, basis = last, "hold the last logged load (moderate readiness: no increases)"
    else:
        proposed, basis = last, "hold the last logged load (not every set hit its rep target)"
    limit = interlocks.clamp_to_jump(float("inf"), last, max_jump_pct)
    if proposed <= last:
        limit = min(limit, last)
    if last < 2 * step:  # finer than the plate grid: rounding would give 0 or flip a progression into a drop
        return min(proposed, limit), basis
    load = _round(min(proposed, limit), step)
    while load > limit + 1e-9:  # rounding must never break the cap
        load -= step
    return load, basis


# ── plan builders ───────────────────────────────────────────────────────────
def _from_template(template: SessionResult, auto: dict, max_jump_pct: float, day: date,
                   sessions: list[SessionResult] | None = None) -> list[ProposedExercise]:
    age = (day - template.local_day).days
    too_old = age > ANCHOR_MAX_AGE_DAYS
    out = []
    for name, rows in _by_exercise(template.sets).items():
        performed = [r for r in rows if r.reps]
        targets = [r.target for r in rows if r.target]
        row_loads = [r.load for r in performed if r.load]
        mid = _lifted_median(row_loads) if row_loads else None
        # a ramp (12 x 20 lb, 10 x 40, 6 x 80) has no one rep target: take the one set(s) at the working load, not the first
        near = [r.target for r in rows if r.target and r.load and mid and abs(r.load - mid) <= 0.1 * mid]
        reps = Counter(near or targets).most_common(1)[0][0] if targets else None
        n_sets = len(targets) or len(rows)
        if auto["volume_pct"] < 0:
            pair = 2 if any(r.side for r in rows) else 1  # a one-sided lift is saved in left/right pairs
            n_sets = pair * max(1, round(n_sets / pair * (1 + auto["volume_pct"] / 100.0)))
        loads = [r.load for r in performed if r.load]
        if too_old:
            out.append(ProposedExercise(name, n_sets, reps, auto["rir_min"], "as_template", None,
                                        f"last logged {age} days ago (over {ANCHOR_MAX_AGE_DAYS}): too old to anchor, "
                                        "choose a load that leaves the target RIR"))
            continue
        if loads and side_missing(rows):
            out.append(ProposedExercise(name, n_sets, reps, auto["rir_min"], "as_template", None,
                                        "one side has no completed sets last time: choose a load that leaves the target RIR"))
            continue
        if not loads:
            note = ("timed exercise (planned by duration, not reps): use your template's targets"
                    if any(getattr(r, "seconds", None) for r in rows)
                    else "no recorded sets last time (skipped or unrecorded): use your template's targets")
            out.append(ProposedExercise(name, n_sets, reps, auto["rir_min"], "as_template", None, note))
            continue
        done_reps = _working_reps(performed)
        if reps and done_reps and _mid(done_reps) < 0.6 * reps:  # a 1-rep max is not the load for a 10-rep prescription
            out.append(ProposedExercise(name, n_sets, reps, auto["rir_min"], "as_template", None,
                                        f"last logged at about {_mid(done_reps)} rep(s) per set, far from the {reps} prescribed: "
                                        "choose a load that leaves the target RIR"))
            continue
        last = anchor_load(performed)
        if last is None:  # the sets that carry a load did not all count (a seconds-only side, a set without reps)
            out.append(ProposedExercise(name, n_sets, reps, auto["rir_min"], "as_template", None,
                                        "no completed set carries a usable load: choose a load that leaves the target RIR"))
            continue
        newer_note = ""
        hit_all = bool(targets) and all((r.reps or 0) >= r.target for r in rows if r.target)
        newer = _newest_logged(name, [x for x in (sessions or []) if x.started_at_ms > template.started_at_ms and x.local_day <= day],
                               since=day - timedelta(days=ANCHOR_MAX_AGE_DAYS))
        if newer is not None:
            nsets, nday = newer
            if side_missing(nsets):  # the latest word is a half-done one-sided lift: the old template's load is not an anchor
                out.append(ProposedExercise(name, n_sets, reps, auto["rir_min"], "as_template", None,
                                            f"a newer session ({nday.isoformat()}) has a side with no completed sets: choose a load that leaves the target RIR"))
                continue
            nload = anchor_load(nsets)
            if nload is None:  # the latest session logged a load but no completed set: that is not a success to build on
                out.append(ProposedExercise(name, n_sets, reps, auto["rir_min"], "as_template", None,
                                            f"a newer session ({nday.isoformat()}) logged this lift with no completed set: choose a load that leaves the target RIR"))
                continue
            if not any(x.reps for x in nsets):  # logged as a timed exercise, whatever its load: no evidence for a rep prescription
                out.append(ProposedExercise(name, n_sets, reps, auto["rir_min"], "as_template", None,
                                            f"a newer session ({nday.isoformat()}) logged this lift as a timed exercise (seconds): choose a load that leaves the target RIR"))
                continue
            nreps = sorted(x.reps for x in nsets if x.reps)
            if nload <= last and _mid(nreps) < 0.3 * (reps or 0):  # 1 or 2 reps of a 10-rep prescription is not a load to plan on
                out.append(ProposedExercise(name, n_sets, reps, auto["rir_min"], "as_template", None,
                                            f"a newer session ({nday.isoformat()}) managed about {_mid(nreps)} rep(s) per set, far from the {reps} prescribed: "
                                            "choose a load that leaves the target RIR"))
                continue
            if nload <= last:  # not heavier since this template: that is where the athlete is now,
                ntargets = [x.target for x in nsets if x.target]  # and that session's result decides whether to progress
                last, newer_note = nload, f"; a newer session ({nday.isoformat()}) is the latest evidence"
                # a success is judged against what this plan prescribes: a 1-rep set at a 1-rep target is not "hit all" for 10 reps
                hit_all = bool(ntargets) and all((x.reps or 0) >= max(x.target, reps or 0) for x in nsets if x.target)
            else:  # heavier than the template: if it missed its reps there, that load is not ready, whatever the template did
                ntargets = [x.target for x in nsets if x.target]
                if ntargets and not all((x.reps or 0) >= max(x.target, reps or 0) for x in nsets if x.target):
                    hit_all, newer_note = False, f"; a heavier newer session ({nday.isoformat()}) at {nload:g} missed its reps"
        load, basis = _progress(last, hit_all, auto["band"], auto["intensity_pct"], max_jump_pct,
                                allow_increase=age < LAYOFF_DAYS, step=_grid(performed))
        uneven = asymmetry_note(performed)
        out.append(ProposedExercise(name, n_sets, reps, auto["rir_min"], "as_template", load,
                                    f"{basis} (last {last:g}{'; ' + uneven if uneven else ''}{newer_note})", last_load=last))
    return out


def _from_catalog(sessions: list[SessionResult], auto: dict, goal: str, days_per_week: int, experience: str,
                  day: date, max_jump_pct: float, warnings: list[str]) -> tuple[str, list[ProposedExercise]]:
    program = coach.generate_program(goal=goal, days_per_week=days_per_week, experience=experience)
    days = program["days"]
    window = {s.local_day for s in sessions if day - timedelta(days=14) <= s.local_day < day}
    todays = days[len(window) % len(days)]
    out = []
    for ex in todays["exercises"]:
        sets = max(1, round(ex["sets"] * (1 + min(0, auto["volume_pct"]) / 100.0)))
        mode = ex["mode"]
        if mode not in interlocks.ALLOWED_MODES:
            warnings.append(f"{ex['name']}: coach suggested {mode!r}; held to standard until done by hand")
            mode = "standard"
        anchor = last_logged(ex["name"], sessions, since=day - timedelta(days=ANCHOR_MAX_AGE_DAYS))
        if anchor is None:
            out.append(ProposedExercise(ex["name"], sets, ex["reps"], max(ex["rir"], auto["rir_min"]), mode, None,
                                        "no logged history for this exercise: choose a load that leaves the target RIR"))
            continue
        last, logged, last_day = anchor
        if not any(x.reps for x in logged):  # logged as a timed exercise (seconds): that is no evidence for a rep prescription
            out.append(ProposedExercise(ex["name"], sets, ex["reps"], max(ex["rir"], auto["rir_min"]), mode, None,
                                        f"last logged on {last_day.isoformat()} as a timed exercise (seconds): choose a load that leaves the target RIR"))
            continue
        done_reps = _working_reps(logged)
        if done_reps and _mid(done_reps) < 0.6 * ex["reps"]:  # a 1-rep max is not the load for 9 reps
            out.append(ProposedExercise(ex["name"], sets, ex["reps"], max(ex["rir"], auto["rir_min"]), mode, None,
                                        f"last logged at about {_mid(done_reps)} rep(s) per set on {last_day.isoformat()}, far from the {ex['reps']} prescribed: "
                                        "choose a load that leaves the target RIR"))
            continue
        hit_all = all(x.reps is not None and x.reps >= ex["reps"] for x in logged)
        load, basis = _progress(last, hit_all, auto["band"], auto["intensity_pct"], max_jump_pct,
                                allow_increase=(day - last_day).days < LAYOFF_DAYS, step=_grid(logged))
        out.append(ProposedExercise(ex["name"], sets, ex["reps"], max(ex["rir"], auto["rir_min"]), mode, load,
                                    f"{basis} (last {last:g} on {last_day.isoformat()}"
                                    f"{'; ' + asymmetry_note(logged) if asymmetry_note(logged) else ''})", last_load=last))
    return todays["name"], out


def todays_plan(*, sessions: list[SessionResult] | None, readings: list[Reading], now: datetime,
                feed_errors: dict[str, str] | None = None, tz: str = DEFAULT_TZ, goal: str = "muscle",
                days_per_week: int = 4, experience: str = "intermediate", template: str | None = None,
                max_jump_pct: float = interlocks.DEFAULT_MAX_JUMP_PCT,
                max_reading_age_h: float = interlocks.DEFAULT_MAX_READING_AGE_H) -> Plan | Unavailable:
    reasons: list[str] = [f"{src} unavailable: {why}" for src, why in sorted((feed_errors or {}).items())]
    if sessions is None and not feed_errors:
        reasons.append("training history was not provided")
    fresh, stale = interlocks.split_fresh(readings, now, max_reading_age_h)
    fresh = [r for r in fresh if r.kind in READINESS_KINDS]
    day = local_today(tz, now)
    future = sorted({s.local_day for s in sessions or [] if s.local_day > day})
    if future:  # a vendor clock/unit bug would otherwise silently become the load anchor
        reasons.append(f"history has {len(future)} future-dated day(s) (e.g. {future[-1].isoformat()}): "
                       "training data is not trustworthy")
    if not fresh:
        why = "no readiness input"
        if stale:
            why = "readiness input is stale (" + ", ".join(
                f"{r.kind} from {r.as_of.isoformat(timespec='minutes')}" for r in stale) + f"; limit {max_reading_age_h:g} h)"
        reasons.append(why)
    if reasons:
        return Unavailable(tuple(reasons))

    sessions = sessions or []
    readiness, provenance = _readiness_dict(fresh)
    auto = coach.autoregulate(readiness)

    warnings: list[str] = []
    for r in stale:  # a dropped reading may be the bad news (e.g. low recovery), so it must be visible
        shown = f"{r.value:g}" if isinstance(r.value, (int, float)) and not isinstance(r.value, bool) else str(r.value)
        warnings.append(f"ignored stale {r.kind} {shown} from {r.as_of.isoformat(timespec='minutes')} "
                        f"(older than {max_reading_age_h:g} h)")
    by_kind: dict[str, list[Reading]] = {}
    for r in fresh:
        by_kind.setdefault(r.kind, []).append(r)
    for kind, rs in sorted(by_kind.items()):
        if len({r.value for r in rs}) > 1:
            newest = max(rs, key=lambda r: r.as_of)
            warnings.append(f"{kind}: readings disagree ({', '.join(f'{r.value} {r.source}' for r in rs)}); "
                            f"used the newest ({newest.value} from {newest.source})")
    trained_today = [s for s in sessions if s.local_day == day]
    if trained_today:
        warnings.append(f"already logged {len(trained_today)} session(s) today")
    if len({s.local_day for s in sessions if day - timedelta(days=7) <= s.local_day <= day}) >= 6:
        warnings.append("6+ training days in the last 7: consider a rest day")

    last_any = max((s.local_day for s in sessions), default=None)
    if last_any is not None and (day - last_any).days >= LAYOFF_DAYS:
        warnings.append(f"{(day - last_any).days} days since your last logged session: no load increases, "
                        "start conservatively")
    title = next_template_title(sessions, day, override=template)
    source, day_name, exercises, chosen = "coach_catalog", "", [], None
    if template and title is None:
        return Unavailable((f"no logged session matches template {template!r}",))
    if title is not None:
        chosen = newest_with_title(sessions, title)
        if chosen is not None and chosen.sets:
            source, day_name = "user_template", title
            exercises = _from_template(chosen, auto, max_jump_pct, day, sessions)
        else:
            warnings.append(f"no per-set detail loaded for your template {title!r}; fell back to the generic program")
    if source == "coach_catalog":
        day_name, exercises = _from_catalog(sessions, auto, goal, days_per_week, experience, day, max_jump_pct, warnings)

    violations = interlocks.validate_exercises(exercises, max_jump_pct=max_jump_pct)
    if violations:  # a violation here is a planner bug: refuse rather than emit an unsafe proposal
        return Unavailable(tuple(f"interlock {v.code}: {v.exercise}: {v.detail}" for v in violations))

    return Plan(local_day=day, band=auto["band"], session=auto["session"], message=auto["message"],
                signals=tuple(auto["signals"]), overrides=tuple(auto["overrides"]), day_name=day_name,
                exercises=tuple(exercises),
                inputs={"readiness": provenance, "sessions_considered": len(sessions),
                        "last_session_day": max((s.local_day for s in sessions), default=None),
                        "template": (f"{title!r} (last done {chosen.local_day})" if chosen else "none"),
                        "rotation": "next = recurring template that has gone longest without being done"},
                warnings=tuple(warnings), template_title=title if source == "user_template" else None, source=source)
