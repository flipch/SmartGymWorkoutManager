"""Characterization (golden) tests for coach.py: pin CURRENT behaviour before anyone refactors it.

These do not claim the coaching is right; they claim it has not silently changed. If you change coach.py
on purpose, regenerate with:  UPDATE_GOLDEN=1 pytest tests/test_coach_characterization.py
and review the diff of tests/fixtures/coach_golden.json like any other code change.
"""
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import coach

GOLDEN = Path(__file__).parent / "fixtures" / "coach_golden.json"

READINESS_CASES = [
    {},
    {"whoop_recovery": 90}, {"whoop_recovery": 67}, {"whoop_recovery": 66}, {"whoop_recovery": 34}, {"whoop_recovery": 33},
    {"hrv_vs_baseline": "above"}, {"hrv_vs_baseline": "below"},
    {"sleep_hours": 5.5}, {"sleep_hours": 8}, {"rhr_delta_bpm": 6}, {"subjective": 2}, {"subjective": 5},
    {"whoop_recovery": 90, "sleep_hours": 5}, {"whoop_recovery": 90, "rhr_delta_bpm": 5},
    {"whoop_recovery": 80, "sleep_hours": 8, "subjective": 5, "acwr": 1.8},
    {"whoop_recovery": 20, "sleep_hours": 8, "subjective": 5},
]
PROGRAM_CASES = [("muscle", 4, "intermediate"), ("strength", 3, "beginner"), ("fatloss", 5, "advanced"),
                 ("general", 2, "intermediate"), ("muscle", 1, "beginner")]


def program_summary(goal, days, exp):
    p = coach.generate_program(goal=goal, days_per_week=days, experience=exp)
    return {"goal": p["goal"], "days_per_week": p["days_per_week"], "experience": p["experience"], "split": p["split"],
            "prescription": p["prescription"], "weekly_sets_per_muscle": p["weekly_sets_per_muscle"],
            "days": [{"name": d["name"], "setup_swaps": d["setup_swaps"],
                      "exercises": [[e["name"], e["sets"], e["reps"], e["rir"], e["mode"]] for e in d["exercises"]]}
                     for d in p["days"]]}


def current_behaviour():
    return {"autoregulate": [[case, coach.autoregulate(case)] for case in READINESS_CASES],
            "programs": [[list(c), program_summary(*c)] for c in PROGRAM_CASES]}


class CoachGolden(unittest.TestCase):
    def test_behaviour_matches_golden(self):
        now = json.loads(json.dumps(current_behaviour(), sort_keys=True))
        if os.environ.get("UPDATE_GOLDEN") == "1":
            GOLDEN.parent.mkdir(exist_ok=True)
            GOLDEN.write_text(json.dumps(now, indent=1, sort_keys=True) + "\n")
        self.assertTrue(GOLDEN.exists(), "golden file missing: run with UPDATE_GOLDEN=1 once and review it")
        self.assertEqual(now, json.loads(GOLDEN.read_text()))

    def test_autoregulate_without_signals_defaults_to_moderate(self):
        """Documented hazard: the planner must gate BEFORE calling this, because no data reads as 'train as planned'."""
        out = coach.autoregulate({})
        self.assertEqual((out["band"], out["session"]), ("moderate", "as_planned"))

    def test_most_conservative_signal_wins(self):
        self.assertEqual(coach.autoregulate({"whoop_recovery": 95, "subjective": 1})["band"], "low")

    def test_sleep_or_rhr_guardrail_caps_a_green_day(self):
        self.assertEqual(coach.autoregulate({"whoop_recovery": 95, "sleep_hours": 5})["band"], "low")  # sleep vote is low
        out = coach.autoregulate({"whoop_recovery": 95, "rhr_delta_bpm": 5})
        self.assertNotEqual(out["band"], "high")


if __name__ == "__main__":
    unittest.main()
