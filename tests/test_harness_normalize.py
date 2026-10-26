"""Normalization + local-day behaviour. Synthetic data only."""
import os
import sys
import unittest
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import synth
from harness.errors import SchemaDrift
from harness.localday import local_day, today
from harness.normalize import session_header, session_with_sets, set_load


class LocalDay(unittest.TestCase):
    def test_evening_lift_stays_on_its_local_day(self):
        # 2026-10-25 18:30 Pacific is 2026-10-26 01:30 UTC; a UTC date would put it on the 26th.
        ms = synth.ts_ms(2026, 10, 25, 18, 30)
        self.assertEqual(datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date(), date(2026, 10, 26))
        self.assertEqual(local_day(ms), date(2026, 10, 25))

    def test_seconds_and_milliseconds_agree(self):
        ms = synth.ts_ms(2026, 3, 8, 12)
        self.assertEqual(local_day(ms), local_day(ms / 1000))

    def test_today_uses_local_calendar_and_requires_aware_now(self):
        now = datetime(2026, 10, 26, 3, 0, tzinfo=timezone.utc)  # still Oct 25 evening in Pacific
        self.assertEqual(today(now=now), date(2026, 10, 25))
        with self.assertRaises(ValueError):
            today(now=datetime(2026, 10, 26, 3, 0))


class SetLoad(unittest.TestCase):
    def test_uniform_weights(self):
        row = synth._set_row(8, 40)
        self.assertEqual(set_load(row), 40.0)

    def test_median_is_robust_to_midset_changes(self):
        row = synth._set_row(5, 40)
        row["trainingInfoDetail"]["weights"] = [40.0, 40.0, 40.0, 20.0, 60.0]
        self.assertEqual(set_load(row), 40.0)

    def test_unilateral_uses_the_trained_side(self):
        row = synth._set_row(6, 30, left_right=2)
        row["trainingInfoDetail"]["weights"] = []
        row["trainingInfoDetail"]["rightWeights"] = [32.0] * 6
        row["trainingInfoDetail"]["leftWeights"] = [99.0] * 6
        self.assertEqual(set_load(row), 32.0)

    def test_falls_back_to_capacity_over_reps_then_none(self):
        row = synth._set_row(10, 25)
        row["trainingInfoDetail"] = {}
        self.assertEqual(set_load(row), 25.0)
        row["finishedCount"] = 0
        self.assertIsNone(set_load(row))


class Sessions(unittest.TestCase):
    def test_header_has_no_sets_and_uses_local_day(self):
        rec = synth.history_record(7, synth.ts_ms(2026, 10, 25, 18, 30), title="Push A")
        h = session_header(rec)
        self.assertEqual((h.source_id, h.title, h.local_day, h.sets), ("7", "Push A", date(2026, 10, 25), ()))

    def test_with_sets_maps_reps_loads_and_leaves_unit_unknown(self):
        rec = synth.history_record(9, synth.ts_ms(2026, 10, 20))
        detail = [synth.exercise("Synthetic Press", [(8, 40), (8, 40), (6, 42.5)])]
        s = session_with_sets(rec, detail)
        self.assertEqual([(x.reps, x.load) for x in s.sets], [(8, 40.0), (8, 40.0), (6, 42.5)])
        self.assertTrue(all(x.unit is None for x in s.sets))
        self.assertTrue(all(x.unit == "kg" for x in session_with_sets(rec, detail, unit="kg").sets))

    def test_timed_exercise_reports_seconds_not_reps(self):
        rec = synth.history_record(10, synth.ts_ms(2026, 10, 20))
        s = session_with_sets(rec, [synth.exercise("Synthetic Plank", [(1, 1)], timed=True)])
        self.assertIsNone(s.sets[0].reps)
        self.assertEqual(s.sets[0].seconds, 45)

    def test_schema_drift_is_loud(self):
        rec = synth.history_record(11, synth.ts_ms(2026, 10, 20))
        with self.assertRaises(SchemaDrift):
            session_with_sets(rec, {"not": "a list"})
        with self.assertRaises(SchemaDrift):
            session_with_sets(rec, [{"name": "renamed field"}])
        with self.assertRaises(SchemaDrift):
            session_header({"title": "no ids"})


if __name__ == "__main__":
    unittest.main()
