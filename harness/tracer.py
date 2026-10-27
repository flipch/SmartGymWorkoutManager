"""Read-only tracer bullet:  recent Speediance sessions + a readiness input  ->  today's proposed workout.

  python -m harness.tracer --whoop-recovery 62 --sleep-hours 7.2                      # live read (cached NANO token)
  python -m harness.tracer --recordings DIR --as-of 2026-09-28T07:00:00-07:00 --subjective 4   # offline replay

Live reads: one history call, one detail read for the chosen template, up to NEWER_DETAIL_MAX (3) more for sessions NEWER
than that template (a newer session can overrule it), and --detail N adds other newest sessions up to N reads in total.

Prints a proposal. It never writes to Speediance and has no push path. Exit codes: 0 plan, 2 unavailable
(a missing/stale/errored input — fix the input, don't override), 1 usage/config error.
"""
import argparse
import dataclasses
import glob
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import localsecrets, planner, readiness
from .errors import SourceDown
from .localday import DEFAULT_TZ
from .model import Plan, SessionResult, Unavailable
from .normalize import session_header, session_with_sets
from .speediance import DETAIL_ENDPOINT, SpeedianceNano


NEWER_DETAIL_MAX = 3  # sessions newer than the chosen template whose sets are read (the protection against an old, too-heavy anchor)


def sessions_from_records(records: list[dict], details: dict[int, list], tz: str, unit: str | None) -> list[SessionResult]:
    """Headers for every record; per-set detail where we have it (newest first)."""
    out = []
    for rec in records:
        tid = rec.get("trainingId")
        out.append(session_with_sets(rec, details[tid], tz, unit) if tid in details else session_header(rec, tz))
    return sorted(out, key=lambda s: s.started_at_ms, reverse=True)


def load_live(now: datetime, tz: str, days: int, detail_n: int, unit: str | None,
              template: str | None) -> list[SessionResult]:
    """Phase 1: one history read (headers). Phase 2: per-set detail ONLY for the template we are about to
    propose (plus up to detail_n-1 other newest sessions) — keeps live calls to ~2 per run."""
    adapter = SpeedianceNano(tz=tz)
    end = now.astimezone(ZoneInfo(tz)).date()
    records = adapter.history(end - timedelta(days=days), end)
    headers = sessions_from_records(records, {}, tz, unit)
    wanted: list[dict] = []
    title = planner.next_template_title(headers, end, override=template)
    if title is not None:
        newest = planner.newest_with_title(headers, title)
        wanted += [r for r in records if str(r.get("trainingId")) == newest.source_id]
        started = {h.source_id: h.started_at_ms for h in headers}  # a newer session may say the old template's load is too heavy
        newer = sorted((r for r in records if r.get("type") in DETAIL_ENDPOINT and r not in wanted
                        and started.get(str(r.get("trainingId")), 0) > newest.started_at_ms),
                       key=lambda r: started.get(str(r.get("trainingId")), 0), reverse=True)
        wanted += newer[:NEWER_DETAIL_MAX]
    others = sorted((r for r in records if r.get("type") in DETAIL_ENDPOINT and r not in wanted),
                    key=lambda r: r.get("startTimestamp") or 0, reverse=True)
    wanted += others[: max(0, detail_n - len(wanted))]
    details = {r["trainingId"]: adapter.detail(r["trainingId"], r["type"])
               for r in wanted if r.get("type") in DETAIL_ENDPOINT}
    return sessions_from_records(records, details, tz, unit)


def load_recordings(directory: Path, tz: str, unit: str | None) -> list[SessionResult]:
    records = json.loads((directory / "history.json").read_text())["records"]
    details = {}
    for path in glob.glob(str(directory / "detail-*.json")):
        blob = json.loads(Path(path).read_text())
        details[blob["record"]["trainingId"]] = blob["detail"]
    return sessions_from_records(records, details, tz, unit)


def render(result: Plan | Unavailable) -> str:
    if isinstance(result, Unavailable):
        return "NO PLAN — inputs missing or unsafe:\n" + "\n".join(f"  - {r}" for r in result.reasons)
    origin = "your template" if result.source == "user_template" else "GENERIC program (no usable template history)"
    lines = [f"{result.local_day.isoformat()}  {result.day_name}  [{origin}]  |  readiness: {result.band.upper()}  |  {result.session}",
             f"  {result.message}"]
    lines += [f"  signal: {s}" for s in result.signals] + [f"  override: {o}" for o in result.overrides]
    lines.append("")
    for ex in result.exercises:
        load = f"{ex.load:g}" if ex.load is not None else "choose by RIR"
        reps = ex.reps if ex.reps is not None else "template reps"
        lines.append(f"  {ex.name}: {ex.sets} x {reps} @ {load}  (leave {ex.rir}+ reps in reserve)")
        lines.append(f"      load: {ex.load_basis}")
    lines.append("")
    lines += [f"  warning: {w}" for w in result.warnings]
    lines.append(f"  inputs: template {result.inputs['template']}; {result.inputs['sessions_considered']} sessions considered"
                 f"; last session {result.inputs['last_session_day']}")
    lines += [f"  from: {p}" for p in result.inputs["readiness"]]
    lines.append("  (proposal only — nothing was sent to the machine; units are whatever your app displays)")
    return "\n".join(lines)


def _jsonable(obj):
    if dataclasses.is_dataclass(obj):
        return {k: _jsonable(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    return obj


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--whoop-live", action="store_true",
                   help="read today's recovery + sleep from WHOOP (run `python -m harness.whoop_auth` once first)")
    p.add_argument("--whoop-recovery", type=float)
    p.add_argument("--sleep-hours", type=float)
    p.add_argument("--rhr-delta", type=float, help="resting HR vs baseline, bpm")
    p.add_argument("--hrv", choices=readiness.HRV_VALUES)
    p.add_argument("--subjective", type=float, help="1-5")
    p.add_argument("--readiness-file", help="JSON with per-signal as_of timestamps")
    p.add_argument("--recordings", help="offline replay from a harness.record directory (no network)")
    p.add_argument("--as-of", help="ISO timestamp with offset to use as 'now' (default: real now)")
    p.add_argument("--tz", default=DEFAULT_TZ)
    p.add_argument("--goal", default="muscle", choices=sorted(__import__("coach").GOALS))
    p.add_argument("--days-per-week", type=int, default=4, choices=range(1, 6))
    p.add_argument("--experience", default="intermediate", choices=sorted(__import__("coach").EXPERIENCE))
    p.add_argument("--unit", choices=("kg", "lb"), default=os.environ.get("GYM_UNIT", "lb"),
                   help="how loads are LABELLED (default: $GYM_UNIT, else lb, the unit Felipe's app shows). "
                        "Never inferred from the vendor, and the planner works in ratios so it cannot change a load")
    p.add_argument("--history-days", type=int, default=28)
    p.add_argument("--detail", type=int, default=1, help="live mode: max per-session detail reads (0-5) for the template plus other newest sessions; up to 3 extra reads of sessions NEWER than the template are always made")
    p.add_argument("--template", help="plan this template instead of the rotation pick (substring of its title)")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    if a.unit not in ("kg", "lb"):  # a bad $GYM_UNIT default is not checked by argparse
        print("error: GYM_UNIT must be kg or lb", file=sys.stderr)
        return 1
    if not 0 <= a.detail <= 5:
        p.error("--detail must be 0..5")

    try:
        ZoneInfo(a.tz)
        now = datetime.fromisoformat(a.as_of) if a.as_of else datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise ValueError("--as-of must include a timezone offset")
        readings = readiness.from_manual(now, whoop_recovery=a.whoop_recovery, sleep_hours=a.sleep_hours,
                                         rhr_delta_bpm=a.rhr_delta, hrv_vs_baseline=a.hrv, subjective=a.subjective)
        if a.readiness_file:
            readings += readiness.from_file(a.readiness_file)
    except (ValueError, KeyError, TypeError, OSError, ZoneInfoNotFoundError) as err:  # bad input is not a crash
        print(f"error: {type(err).__name__}: {err}", file=sys.stderr)
        return 1

    sessions, errors, whoop_notes = None, {}, []
    if a.whoop_live:  # a WHOOP failure is an UNAVAILABLE plan, never a silent fall-back to "no readiness data"
        try:
            from . import whoop_client
            live, notes = whoop_client.default_client().fetch_readings()
            readings += live
            for note in notes:
                print(f"note: whoop: {note}", file=sys.stderr)
                whoop_notes.append(note)
        except (SourceDown, OSError, ValueError) as err:
            errors["whoop"] = str(err) if isinstance(err, SourceDown) else f"{type(err).__name__} reading WHOOP credentials"
    try:
        if a.recordings:
            sessions = load_recordings(Path(a.recordings), a.tz, a.unit)
        else:
            sessions = load_live(now, a.tz, a.history_days, a.detail, a.unit, a.template)
    except (SourceDown, PermissionError, FileNotFoundError, KeyError, ValueError) as err:
        errors["speediance"] = str(err)
    except Exception as err:  # noqa: BLE001 - a drifted payload must end in Unavailable (exit 2), never a traceback
        errors["speediance"] = f"unexpected {type(err).__name__} while reading history (likely schema drift)"

    result = planner.todays_plan(sessions=sessions, readings=readings, now=now, feed_errors=errors, tz=a.tz,
                                 goal=a.goal, days_per_week=a.days_per_week, experience=a.experience, template=a.template)
    if whoop_notes and isinstance(result, Plan):  # a missing/unscored recovery must be visible in the plan, not only on stderr
        result = dataclasses.replace(result, warnings=result.warnings + tuple(f"whoop: {n}" for n in whoop_notes))
    print(json.dumps(_jsonable(result), indent=2) if a.json else render(result))
    return 2 if isinstance(result, Unavailable) else 0


if __name__ == "__main__":
    sys.exit(main())
