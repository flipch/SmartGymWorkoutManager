"""Manual / file readiness input -> Reading objects. (WHOOP will plug in here as another source.)

CLI-supplied values count as fresh "now" from source "manual". A readiness file keeps per-signal timestamps so
stale inputs can be rejected:  {"whoop_recovery": {"value": 62, "as_of": "2026-10-26T06:40:00-07:00", "source": "whoop"}}
"""
import json
from datetime import datetime
from pathlib import Path

from .model import READINESS_KINDS, Reading

HRV_VALUES = ("above", "within", "below")


def from_manual(now: datetime, *, whoop_recovery=None, sleep_hours=None, rhr_delta_bpm=None,
                hrv_vs_baseline=None, subjective=None) -> list[Reading]:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    raw = {"whoop_recovery": whoop_recovery, "sleep_hours": sleep_hours, "rhr_delta_bpm": rhr_delta_bpm,
           "hrv_vs_baseline": hrv_vs_baseline, "subjective": subjective}
    out = []
    for kind, value in raw.items():
        if value is None:
            continue
        _check(kind, value)
        out.append(Reading(kind, value, "manual", now))
    return out


def from_file(path) -> list[Reading]:
    blob = json.loads(Path(path).read_text())
    out = []
    for kind, item in blob.items():
        if kind not in READINESS_KINDS:
            raise ValueError(f"unknown readiness kind {kind!r}")
        as_of = datetime.fromisoformat(item["as_of"])
        if as_of.tzinfo is None:
            raise ValueError(f"{kind}.as_of must carry a timezone offset")
        _check(kind, item["value"])
        out.append(Reading(kind, item["value"], item.get("source", "file"), as_of))
    return out


def _check(kind: str, value) -> None:
    ranges = {"whoop_recovery": (0, 100), "sleep_hours": (0, 16), "rhr_delta_bpm": (-30, 30),
              "subjective": (1, 5), "acwr": (0, 5)}
    if kind == "hrv_vs_baseline":
        if value not in HRV_VALUES:
            raise ValueError(f"hrv_vs_baseline must be one of {HRV_VALUES}")
        return
    lo, hi = ranges[kind]
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not lo <= value <= hi:
        raise ValueError(f"{kind} must be a number in [{lo}, {hi}], got {value!r}")
