"""Fixture-vs-reality contract.

Synthetic payloads (tests/synth.py) must (1) contain every key the normalizer depends on and (2) invent
no field that real data lacks. The real side must (3) carry the required keys in EVERY recorded record.

Real recordings (harness.record) live outside the repo, so these tests skip where recordings are absent
(CI, a fresh clone) and run on the developer's machine. Measured on 2026-10-26: many keys are optional in
real data (e.g. `trainingInfoDetail` missing on some sets, course-type history rows have different keys),
which is why the check is "required subset" + "no invented keys", not equality.
"""
import glob
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import synth

REC = Path(os.environ.get("HARNESS_RECORDINGS", Path.home() / ".config/speediance-spike/recordings"))

# What harness/normalize.py + the planner actually read.
REQUIRED_HISTORY = {"trainingId", "startTimestamp", "title", "trainingTime", "totalCapacity", "type"}
REQUIRED_EXERCISE = {"actionLibraryName", "finishedReps", "completionMethod"}
REQUIRED_SET = {"finishedCount", "targetCount", "capacity", "time", "leftRight"}

HAVE_HISTORY = (REC / "history.json").exists()
HAVE_DETAIL = bool(glob.glob(str(REC / "detail-*.json")))


def _real_history():
    return json.loads((REC / "history.json").read_text())["records"]


def _real_details():
    out = []
    for path in glob.glob(str(REC / "detail-*.json")):
        out.append(json.loads(Path(path).read_text())["detail"])
    return out


class SyntheticFixtures(unittest.TestCase):
    """Run everywhere: the synthetic side must contain what the normalizer needs."""

    def test_synthetic_has_required_keys(self):
        self.assertTrue(REQUIRED_HISTORY <= set(synth.history_record(1, 1)))
        ex = synth.exercise("x", [(3, 10)])
        self.assertTrue(REQUIRED_EXERCISE <= set(ex))
        self.assertTrue(REQUIRED_SET <= set(ex["finishedReps"][0]))


class AgainstRealRecordings(unittest.TestCase):
    @unittest.skipUnless(HAVE_HISTORY, "no real recordings on this machine")
    def test_required_history_keys_in_every_real_record(self):
        for rec in _real_history():
            self.assertTrue(REQUIRED_HISTORY <= set(rec), f"missing {REQUIRED_HISTORY - set(rec)}")

    @unittest.skipUnless(HAVE_HISTORY, "no real recordings on this machine")
    def test_synthetic_history_invents_no_keys(self):
        union = set().union(*(set(r) for r in _real_history()))
        self.assertEqual(set(synth.history_record(1, 1)) - union, set())

    @unittest.skipUnless(HAVE_DETAIL, "no real detail recordings on this machine")
    def test_required_detail_keys_in_every_real_exercise_and_set(self):
        for detail in _real_details():
            self.assertIsInstance(detail, list)
            for ex in detail:
                self.assertTrue(REQUIRED_EXERCISE <= set(ex), f"missing {REQUIRED_EXERCISE - set(ex)}")
                for row in ex["finishedReps"]:
                    self.assertTrue(REQUIRED_SET <= set(row), f"missing {REQUIRED_SET - set(row)}")

    @unittest.skipUnless(HAVE_DETAIL, "no real detail recordings on this machine")
    def test_synthetic_detail_invents_no_keys(self):
        ex_union, row_union, info_union = set(), set(), set()
        for detail in _real_details():
            for ex in detail:
                ex_union |= set(ex)
                for row in ex["finishedReps"]:
                    row_union |= set(row)
                    info_union |= set(row.get("trainingInfoDetail") or {})
        fake = synth.exercise("x", [(3, 10)])
        self.assertEqual(set(fake) - ex_union, set())
        self.assertEqual(set(fake["finishedReps"][0]) - row_union, set())
        self.assertEqual(set(fake["finishedReps"][0]["trainingInfoDetail"]) - info_union, set())

    @unittest.skipUnless(HAVE_DETAIL, "no real detail recordings on this machine")
    def test_normalizer_runs_on_every_real_recording(self):
        from harness.normalize import session_with_sets
        for path in glob.glob(str(REC / "detail-*.json")):
            blob = json.loads(Path(path).read_text())
            s = session_with_sets(blob["record"], blob["detail"])
            self.assertTrue(s.sets, "no sets parsed from a real session")
            self.assertTrue(any(x.load for x in s.sets), "no loads parsed from a real session")
            self.assertTrue(all(x.unit is None for x in s.sets))


if __name__ == "__main__":
    unittest.main()
