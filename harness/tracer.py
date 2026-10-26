"""Read-only tracer bullet:  recent Speediance sessions + a readiness input  ->  today's proposed workout.

  python -m harness.tracer --whoop-recovery 62 --sleep-hours 7.2                      # live read (cached NANO token)
  python -m harness.tracer --recordings DIR --as-of 2026-09-28T07:00:00-07:00 --subjective 4   # offline replay

Prints a proposal. It never writes to Speediance and has no push path. Exit codes: 0 plan, 2 unavailable
(a missing/stale/errored input — fix the input, don't override), 1 usage/config error.
"""
import argparse
import dataclasses
import glob
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import localsecrets, planner, readiness
from .errors import SourceDown
from .localday import DEFAULT_TZ
from .model import Plan, SessionResult, Unavailable
from .normalize import session_header, session_with_sets
from .speediance import DETAIL_ENDPOINT, SpeedianceNano


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
    p.add_argument("--unit", choices=("kg", "lb"), help="only labels output; never inferred")
    p.add_argument("--history-days", type=int, default=28)
    p.add_argument("--detail", type=int, default=1, help="live mode: max per-session detail reads (0-5); 1 = just the chosen template")
    p.add_argument("--template", help="plan this template instead of the rotation pick (substring of its title)")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    if not 0 <= a.detail <= 5:
        p.error("--detail must be 0..5")

    now = datetime.fromisoformat(a.as_of) if a.as_of else datetime.now(timezone.utc)
    if now.tzinfo is None:
        p.error("--as-of must include a timezone offset")
    readings = readiness.from_manual(now, whoop_recovery=a.whoop_recovery, sleep_hours=a.sleep_hours,
                                     rhr_delta_bpm=a.rhr_delta, hrv_vs_baseline=a.hrv, subjective=a.subjective)
    if a.readiness_file:
        readings += readiness.from_file(a.readiness_file)

    sessions, errors = None, {}
    try:
        if a.recordings:
            sessions = load_recordings(Path(a.recordings), a.tz, a.unit)
        else:
            sessions = load_live(now, a.tz, a.history_days, a.detail, a.unit, a.template)
    except (SourceDown, PermissionError, FileNotFoundError, KeyError, ValueError) as err:
        errors["speediance"] = str(err)

    result = planner.todays_plan(sessions=sessions, readings=readings, now=now, feed_errors=errors, tz=a.tz,
                                 goal=a.goal, days_per_week=a.days_per_week, experience=a.experience, template=a.template)
    print(json.dumps(_jsonable(result), indent=2) if a.json else render(result))
    return 2 if isinstance(result, Unavailable) else 0


if __name__ == "__main__":
    sys.exit(main())
