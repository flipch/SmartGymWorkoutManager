"""Offline evaluation of the planner: seeded random histories x readiness inputs, hard invariants, a scorecard.

Run with `pytest tests/test_harness_eval.py -s` to see the scorecard. Every invariant must hold on EVERY trial:
  I1  a returned Plan passes the interlock validator
  I2  no load exceeds the last logged load by more than the jump cap
  I3  an exercise without a logged anchor never has a load
  I4  LOW readiness: every anchored load <= its last load, and sets never increase
  I5  non-HIGH readiness: no anchored load above its last load (rounding tolerance of one plate step)
  I6  missing / stale / errored inputs -> Unavailable (never a plan)
  I7  deterministic: same inputs, same plan
Real-data eval (skips without local recordings): every exercise that has recorded sets gets an anchored load.
"""
import glob
import json
import os
import random
import sys
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import synth
from harness import interlocks, planner
from harness.model import Plan, Reading, Unavailable
from harness.normalize import session_header, session_with_sets

NOW = datetime(2026, 10, 26, 7, 0, tzinfo=synth.PT)
TODAY = NOW.date()
EXERCISES = ["Synthetic Press", "Synthetic Row", "Synthetic Squat", "Synthetic Curl", "Synthetic Fly", "Synthetic Raise"]
TITLES = ["Push", "Pull", "Legs"]
TRIALS = 400
STATS = {"trials": 0, "plans": 0, "unavailable_expected": 0, "anchored": 0, "exercises": 0, "violations": []}


def random_history(rng: random.Random):
    """Three recurring templates, a random subset with per-set detail, random loads/reps/targets."""
    sessions, tid = [], rng.randrange(1, 10**6)
    for title in TITLES:
        names = rng.sample(EXERCISES, rng.randint(2, 5))
        for k in range(rng.randint(2, 4)):
            day = TODAY - timedelta(days=rng.randint(1 + k * 5, 6 + k * 5 + TITLES.index(title)))
            tid += 1
            rec = synth.history_record(tid, synth.ts_ms(day.year, day.month, day.day), title=title)
            with_detail = k == 0 or rng.random() < 0.3
            if not with_detail:
                sessions.append(session_header(rec))
                continue
            detail = []
            for name in names:
                target = rng.choice([5, 6, 8, 10, 12])
                base = round(rng.uniform(5, 150) * 2) / 2
                rows = []
                for _ in range(rng.randint(2, 4)):
                    reps = rng.choice([0, target - 2, target, target, target + 2])
                    rows.append((max(reps, 0), base if reps else 0, target))
                detail.append(synth.exercise(name, rows))
            sessions.append(session_with_sets(rec, detail))
    return sessions


def random_readings(rng: random.Random):
    pool = [("whoop_recovery", rng.uniform(0, 100)), ("sleep_hours", rng.uniform(3, 10)),
            ("rhr_delta_bpm", rng.uniform(-5, 10)), ("subjective", float(rng.randint(1, 5))),
            ("hrv_vs_baseline", rng.choice(["above", "within", "below"]))]
    chosen = rng.sample(pool, rng.randint(1, len(pool)))
    return [Reading(k, v, "manual", NOW - timedelta(minutes=rng.randint(0, 600))) for k, v in chosen]


class PlannerEval(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        s = STATS
        rate = s["anchored"] / s["exercises"] if s["exercises"] else 0
        print(f"\n[eval] trials={s['trials']} plans={s['plans']} gate_ok={s['unavailable_expected']} "
              f"exercises={s['exercises']} anchored={s['anchored']} ({rate:.0%}) violations={len(s['violations'])}")

    def test_safety_invariants_hold_on_every_random_trial(self):
        rng = random.Random(20261026)
        for trial in range(TRIALS):
            sessions, readings = random_history(rng), random_readings(rng)
            result = planner.todays_plan(sessions=sessions, readings=readings, now=NOW)
            again = planner.todays_plan(sessions=sessions, readings=readings, now=NOW)
            STATS["trials"] += 1
            self.assertEqual(result, again, f"I7 nondeterministic (trial {trial})")
            self.assertIsInstance(result, (Plan, Unavailable))
            if isinstance(result, Unavailable):
                STATS["violations"] += [f"trial {trial}: unexpected Unavailable {result.reasons}"]
                self.fail(f"fresh inputs gave Unavailable: {result.reasons}")
            STATS["plans"] += 1
            self.assertEqual(interlocks.validate_exercises(result.exercises), [], f"I1 trial {trial}")
            for ex in result.exercises:
                STATS["exercises"] += 1
                if ex.load is None:
                    continue
                STATS["anchored"] += 1
                self.assertIsNotNone(ex.last_load, f"I3 trial {trial}: {ex.name}")
                self.assertLessEqual(ex.load, ex.last_load * 1.10 + 1e-9, f"I2 trial {trial}: {ex.name}")
                if result.band != "high":
                    self.assertLessEqual(ex.load, ex.last_load + 0.5, f"I5 trial {trial}: {ex.name}")
                if result.band == "low":
                    self.assertLessEqual(ex.load, ex.last_load, f"I4 trial {trial}: {ex.name}")
            for ex in result.exercises:
                if ex.last_load is None:
                    self.assertIsNone(ex.load, f"I3 trial {trial}: {ex.name}")

    def test_low_readiness_never_adds_sets(self):
        rng = random.Random(7)
        checked = 0
        for _ in range(150):
            sessions = random_history(rng)
            low = [Reading("whoop_recovery", rng.uniform(0, 30), "manual", NOW)]
            normal = [Reading("subjective", 3.0, "manual", NOW)]
            a, b = planner.todays_plan(sessions=sessions, readings=low, now=NOW), planner.todays_plan(sessions=sessions, readings=normal, now=NOW)
            if isinstance(a, Plan) and isinstance(b, Plan) and a.day_name == b.day_name:
                checked += 1
                for ea, eb in zip(a.exercises, b.exercises):
                    self.assertLessEqual(ea.sets, eb.sets)
        self.assertGreater(checked, 50)

    def test_gate_is_never_bypassed_by_bad_inputs(self):
        rng = random.Random(99)
        for trial in range(300):
            sessions = random_history(rng)
            mode = rng.choice(["none", "stale", "future", "feed_error", "no_history"])
            readings = random_readings(rng)
            kwargs = {}
            if mode == "none":
                readings = []
            elif mode == "stale":
                readings = [Reading(r.kind, r.value, r.source, NOW - timedelta(hours=rng.randint(19, 200))) for r in readings]
            elif mode == "future":
                readings = [Reading(r.kind, r.value, r.source, NOW + timedelta(hours=rng.randint(1, 48))) for r in readings]
            elif mode == "feed_error":
                kwargs["feed_errors"] = {"speediance": "boom"}
                sessions = None
            elif mode == "no_history":
                sessions = None
            result = planner.todays_plan(sessions=sessions, readings=readings, now=NOW, **kwargs)
            self.assertIsInstance(result, Unavailable, f"I6 trial {trial} mode={mode}")
            STATS["unavailable_expected"] += 1


REC = Path(os.environ.get("HARNESS_RECORDINGS", Path.home() / ".config/speediance-spike/recordings"))


class RealDataEval(unittest.TestCase):
    @unittest.skipUnless((REC / "history.json").exists() and glob.glob(str(REC / "detail-*.json")), "no recordings")
    def test_every_exercise_with_recorded_sets_is_anchored(self):
        from harness import tracer
        sessions = tracer.load_recordings(REC, "America/Los_Angeles", None)
        rd = [Reading("subjective", 3.0, "manual", datetime(2026, 9, 28, 7, 0, tzinfo=synth.PT))]
        now = datetime(2026, 9, 28, 7, 0, tzinfo=synth.PT)
        total = anchored = 0
        for session in [s for s in sessions if s.sets]:
            result = planner.todays_plan(sessions=sessions, readings=rd, now=now, template=session.title)
            self.assertIsInstance(result, Plan, session.title)
            self.assertEqual(result.source, "user_template")
            for ex in result.exercises:
                recorded = any(s.exercise == ex.name and s.reps and s.load
                               for s in planner.newest_with_title(sessions, session.title).sets)
                total += 1
                if recorded:
                    self.assertIsNotNone(ex.load, f"recorded exercise left unanchored: {ex.name}")
                    anchored += 1
                else:
                    self.assertIsNone(ex.load)
        print(f"\n[eval:real] templates evaluated; exercises={total} with-recorded-sets={anchored} (all anchored)")
        self.assertGreater(anchored, 0)


if __name__ == "__main__":
    unittest.main()
