"""Planner behaviour on synthetic data: gating, template choice, load logic, interlocks."""
import os
import sys
import unittest
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import synth
from harness import interlocks, planner
from harness.model import Plan, Reading, Unavailable
from harness.normalize import session_header, session_with_sets

NOW = datetime(2026, 10, 26, 7, 0, tzinfo=synth.PT)


def session(day: date, title, exercises=None, tid=None):
    rec = synth.history_record(tid or int(day.strftime("%Y%m%d")) * 10 + len(title) % 10,
                               synth.ts_ms(day.year, day.month, day.day), title=title)
    if exercises is None:
        return session_header(rec)
    return session_with_sets(rec, exercises)


def push_day(weights=(40, 40, 40), reps=(8, 8, 8), target=8):
    return [synth.exercise("Synthetic Bench Press", [(r, w, target) for r, w in zip(reps, weights)]),
            synth.exercise("Synthetic Chest Fly", [(12, 20, 12)] * 3)]


def readings(*pairs, at=NOW):
    return [Reading(k, v, "manual", at) for k, v in pairs]


def plan(sessions, rd, **kw):
    return planner.todays_plan(sessions=sessions, readings=rd, now=NOW, **kw)


class Gating(unittest.TestCase):
    def test_no_readiness_is_unavailable_not_train(self):
        r = plan([], [])
        self.assertIsInstance(r, Unavailable)
        self.assertFalse(r)
        self.assertIn("no readiness input", r.reasons[0])

    def test_stale_readiness_is_unavailable_and_says_why(self):
        r = plan([], readings(("whoop_recovery", 80), at=NOW - timedelta(hours=30)))
        self.assertIsInstance(r, Unavailable)
        self.assertIn("stale", r.reasons[0])

    def test_future_dated_reading_is_not_trusted(self):
        self.assertIsInstance(plan([], readings(("whoop_recovery", 80), at=NOW + timedelta(hours=2))), Unavailable)

    def test_feed_error_blocks_the_plan_even_with_fresh_readiness(self):
        r = plan(None, readings(("whoop_recovery", 90)), feed_errors={"speediance": "network error: TimeoutError"})
        self.assertIsInstance(r, Unavailable)
        self.assertIn("speediance unavailable", r.reasons[0])

    def test_missing_history_without_a_reason_is_still_unavailable(self):
        self.assertIsInstance(plan(None, readings(("whoop_recovery", 90))), Unavailable)

    def test_unknown_signal_kinds_do_not_count_as_readiness(self):
        self.assertIsInstance(plan([], [Reading("mood_ring", "green", "manual", NOW)]), Unavailable)

    def test_genuinely_empty_history_is_fine(self):
        r = plan([], readings(("subjective", 4)))
        self.assertIsInstance(r, Plan)
        self.assertEqual(r.source, "coach_catalog")


class TemplateChoice(unittest.TestCase):
    def setUp(self):
        d = date(2026, 10, 26)
        self.sessions = [
            session(d - timedelta(days=2), "Push", push_day()),
            session(d - timedelta(days=9), "Push"),
            session(d - timedelta(days=4), "Pull", [synth.exercise("Synthetic Row", [(8, 50, 8)] * 3)]),
            session(d - timedelta(days=11), "Pull"),
            session(d - timedelta(days=20), "Legs"),
            session(d - timedelta(days=30), "Legs"),
            session(d - timedelta(days=3), "One-off"),
        ]

    def test_picks_the_recurring_template_gone_longest_and_ignores_one_offs(self):
        self.assertEqual(planner.next_template_title(self.sessions, date(2026, 10, 26)), "Legs")

    def test_skips_a_template_already_done_today(self):
        s = self.sessions + [session(date(2026, 10, 26), "Legs")]
        self.assertEqual(planner.next_template_title(s, date(2026, 10, 26)), "Pull")

    def test_override_matches_by_substring_and_unknown_override_is_unavailable(self):
        self.assertEqual(planner.next_template_title(self.sessions, date(2026, 10, 26), override="pul"), "Pull")
        r = plan(self.sessions, readings(("subjective", 4)), template="Yoga")
        self.assertIsInstance(r, Unavailable)

    def test_template_without_detail_falls_back_with_a_warning(self):
        r = plan(self.sessions, readings(("subjective", 4)))  # Legs has headers only
        self.assertIsInstance(r, Plan)
        self.assertEqual(r.source, "coach_catalog")
        self.assertTrue(any("no per-set detail" in w for w in r.warnings))


class LoadLogic(unittest.TestCase):
    def sessions(self, **kw):
        d = date(2026, 10, 26)
        return [session(d - timedelta(days=3), "Push", push_day(**kw)), session(d - timedelta(days=10), "Push")]

    def load_of(self, result, name="Synthetic Bench Press"):
        return next(e for e in result.exercises if e.name == name)

    def test_moderate_holds_even_when_targets_were_hit(self):
        r = plan(self.sessions(), readings(("subjective", 3)))
        self.assertEqual((r.band, self.load_of(r).load), ("moderate", 40.0))

    def test_high_and_hit_progresses_two_and_a_half_percent_rounded(self):
        r = plan(self.sessions(), readings(("whoop_recovery", 85), ("subjective", 5)))
        self.assertEqual(r.band, "high")
        self.assertEqual(self.load_of(r).load, 41.0)  # 40 * 1.025 = 41.0

    def test_high_but_a_missed_set_holds(self):
        r = plan(self.sessions(reps=(8, 8, 5)), readings(("whoop_recovery", 85), ("subjective", 5)))
        self.assertEqual(self.load_of(r).load, 40.0)

    def test_low_reduces_load_and_sets_and_never_progresses(self):
        r = plan(self.sessions(), readings(("whoop_recovery", 20)))
        ex = self.load_of(r)
        self.assertEqual(r.band, "low")
        self.assertEqual(ex.load, 34.0)  # 40 * 0.85
        self.assertLess(ex.sets, 3)

    def test_unanchored_exercise_gets_no_load(self):
        d = date(2026, 10, 26)
        sessions = [session(d - timedelta(days=3), "Push", push_day()), session(d - timedelta(days=10), "Push")]
        sessions[0] = session(d - timedelta(days=3), "Push", [synth.exercise("Synthetic Press", [(8, 40, 8)] * 3),
                                                              synth.exercise("Synthetic Skipped", [(0, 0, 0)] * 3)])
        r = plan(sessions, readings(("subjective", 4)))
        skipped = self.load_of(r, "Synthetic Skipped")
        self.assertIsNone(skipped.load)
        self.assertIsNone(skipped.reps)
        self.assertIn("no recorded sets", skipped.load_basis)

    def test_proposal_is_always_marked_as_proposal_only_source(self):
        r = plan(self.sessions(), readings(("subjective", 4)))
        self.assertEqual((r.source, r.template_title), ("user_template", "Push"))
        self.assertTrue(all(e.mode == "as_template" for e in r.exercises))


class HistoryTrust(unittest.TestCase):
    """Review findings: future-dated / ancient / layoff history must not become an unchecked load anchor."""

    def two_pushes(self, ago_a, ago_b, **kw):
        d = date(2026, 10, 26)
        return [session(d - timedelta(days=ago_a), "Push", push_day(**kw)), session(d - timedelta(days=ago_b), "Push")]

    def bench(self, result):
        return next(e for e in result.exercises if e.name == "Synthetic Bench Press")

    def test_a_future_dated_session_makes_the_plan_unavailable(self):
        bad = session(date(2027, 1, 1), "Push", push_day(weights=(500, 500, 500)))
        r = plan(self.two_pushes(3, 10) + [bad], readings(("whoop_recovery", 90), ("subjective", 5)))
        self.assertIsInstance(r, Unavailable)
        self.assertIn("future-dated", r.reasons[0])

    def test_session_later_today_is_not_future(self):
        later = session(date(2026, 10, 26), "Legs")
        self.assertIsInstance(plan(self.two_pushes(3, 10) + [later], readings(("subjective", 3))), Plan)

    def test_ancient_anchor_is_not_trusted_for_a_load(self):
        r = plan(self.two_pushes(400, 410), readings(("subjective", 3)), template="Push")  # explicit pick
        self.assertIsInstance(r, Plan)
        self.assertEqual(r.source, "user_template")
        self.assertIsNone(self.bench(r).load)
        self.assertIn("too old", self.bench(r).load_basis)

    def test_catalog_ignores_loads_older_than_the_anchor_window(self):
        d = date(2026, 10, 26)
        old = session(d - timedelta(days=400), "X", [synth.exercise("Synthetic Press", [(8, 60, 8)] * 3)])
        self.assertIsNotNone(planner.last_logged("Synthetic Press", [old]))
        self.assertIsNone(planner.last_logged("Synthetic Press", [old], since=d - timedelta(days=90)))

    def test_layoff_blocks_increases_even_on_a_green_day(self):
        r = plan(self.two_pushes(20, 27), readings(("whoop_recovery", 90), ("subjective", 5)))
        self.assertEqual(r.band, "high")
        self.assertEqual(self.bench(r).load, 40.0)  # not 41.0
        self.assertTrue(any("no load increases" in w for w in r.warnings))

    def test_recent_green_day_still_progresses(self):
        r = plan(self.two_pushes(3, 10), readings(("whoop_recovery", 90), ("subjective", 5)))
        self.assertEqual(self.bench(r).load, 41.0)

    def test_hold_never_exceeds_the_logged_load_when_off_the_plate_grid(self):
        r = plan(self.two_pushes(3, 10, weights=(20.3, 20.3, 20.3)), readings(("subjective", 3)))
        self.assertLessEqual(self.bench(r).load, 20.3)

    def test_similar_names_that_are_different_movements_do_not_share_an_anchor(self):
        self.assertFalse(planner._same_exercise("Incline Bench Press", "Decline Bench Press"))
        self.assertFalse(planner._same_exercise("Seated Row", "Standing Row"))
        self.assertFalse(planner._same_exercise("Left Curl", "Right Curl"))
        self.assertTrue(planner._same_exercise("Bench Press", "bench  press"))
        self.assertTrue(planner._same_exercise("Cable Row", "Cable Rows"))

    def test_one_word_muscle_or_movement_differences_are_different_exercises(self):
        for a, b in (("Biceps Curl", "Triceps Curl"), ("Triceps Extension", "Biceps Extension"),
                     ("Hip Abduction", "Hip Adduction"), ("Leg Press", "Leg Curl"), ("Shoulder Press", "Chest Press")):
            self.assertFalse(planner._same_exercise(a, b), f"{a} / {b}")

    def test_the_users_real_naming_style_still_matches_itself(self):
        for name in ("Dual-Handle Bench Press", "Standing Tricep Rope Overhead Triceps Extension (Main Frame)",
                     "Kneeling Dual-Handle Triceps Dips", "Standing Dual-Handle Chest Fly"):
            self.assertTrue(planner._same_exercise(name, name.upper().replace("-", " ")), name)
        self.assertFalse(planner._same_exercise("", ""))
        self.assertFalse(planner._same_exercise("(Seated)", "(Standing)"))

    def test_variants_that_differ_only_inside_parentheses_are_different_exercises(self):
        for a, b in (("Calf Raise (Seated)", "Calf Raise (Standing)"), ("Row (Single Arm, Left)", "Row (Single Arm, Right)"),
                     ("Chest Press (Main Frame)", "Chest Press (Seated)"), ("Squat (Barbell)", "Squat (Smith)")):
            self.assertFalse(planner._same_exercise(a, b), f"{a} / {b}")

    def test_plural_and_hyphen_variants_of_the_same_name_still_match(self):
        for a, b in (("Leg Press", "Leg Presses"), ("Abs Crunch", "Ab Crunch"), ("Dual-Handle Bench Press", "Bench Press (Dual Handle)"),
                     ("Kneeling Dips", "Kneeling Dip")):
            self.assertTrue(planner._same_exercise(a, b), f"{a} / {b}")

    def test_a_load_finer_than_the_plate_grid_is_never_rounded_to_zero_or_flipped(self):
        for last in (0.3, 0.74, 0.99):
            for band in ("low", "moderate", "high"):
                load, _ = planner._progress(last, True, band, -15 if band == "low" else 0, 10)
                self.assertGreater(load, 0, (last, band))
                self.assertLessEqual(load, last * 1.10 + 1e-9, (last, band))
                if band != "low":
                    self.assertGreaterEqual(load, last, f"a hold/progression must not become a drop ({last}, {band})")

    def test_catalog_does_not_anchor_on_a_lookalike_exercise(self):
        d = date(2026, 10, 26)
        decline = [synth.exercise("Decline Bench Press", [(8, 100, 8)] * 3)]
        self.assertIsNone(planner.last_logged("Incline Bench Press", [session(d - timedelta(days=2), "X", decline)]))


class CatalogPathTrust(unittest.TestCase):
    """The generic-program fallback must obey the same anchor-age, layoff and jump-cap rules as the template path."""

    @classmethod
    def setUpClass(cls):
        import coach
        cls.days = coach.generate_program(goal="muscle", days_per_week=4, experience="intermediate")["days"]

    def history(self, days_ago, index):
        """One logged session containing day `index`'s first exercise, every set beating the target."""
        ex = self.days[index]["exercises"][0]
        d = date(2026, 10, 26) - timedelta(days=days_ago)
        logged = [synth.exercise(ex["name"], [(ex["reps"] + 2, 40, ex["reps"])] * 3)]
        return ex["name"], [session(d, "One-off", logged)]

    def first(self, result, name):
        return next(e for e in result.exercises if e.name == name)

    GREEN = (("whoop_recovery", 90), ("subjective", 5))

    def test_recent_anchor_progresses_on_a_green_day(self):
        name, sessions = self.history(days_ago=3, index=1)  # one session in the last 14 days -> plans day index 1
        r = plan(sessions, readings(*self.GREEN))
        self.assertEqual(r.source, "coach_catalog")
        self.assertEqual(self.first(r, name).load, 41.0)

    def test_layoff_holds_the_load_on_the_catalog_path(self):
        name, sessions = self.history(days_ago=20, index=0)  # outside the 14-day window -> plans day index 0
        r = plan(sessions, readings(*self.GREEN))
        self.assertEqual(r.band, "high")
        self.assertEqual(self.first(r, name).load, 40.0)

    def test_anchor_older_than_ninety_days_gives_no_load_on_the_catalog_path(self):
        name, sessions = self.history(days_ago=100, index=0)
        r = plan(sessions, readings(*self.GREEN))
        self.assertIsNone(self.first(r, name).load)

    def test_a_tight_jump_cap_binds_even_on_a_progression(self):
        name, sessions = self.history(days_ago=3, index=1)
        r = plan(sessions, readings(*self.GREEN), max_jump_pct=1.0)
        self.assertLessEqual(self.first(r, name).load, 40.0 * 1.01 + 1e-9)
        self.assertEqual(interlocks.validate_exercises(r.exercises, max_jump_pct=1.0), [])


class Warnings(unittest.TestCase):
    def test_already_trained_today_and_overreach_warnings(self):
        d = date(2026, 10, 26)
        sessions = [session(d - timedelta(days=i), "Push" if i % 2 else "Pull") for i in range(0, 7)]
        r = plan(sessions, readings(("subjective", 4)))
        self.assertIsInstance(r, Plan)
        self.assertTrue(any("today" in w for w in r.warnings))
        self.assertTrue(any("6+ training days" in w for w in r.warnings))


class Interlocks(unittest.TestCase):
    def test_validator_flags_each_rule(self):
        from harness.model import ProposedExercise as P
        bad = [P("a", 3, 8, 2, "chain", 50.0, "x", last_load=40.0),   # mode + jump
               P("b", 3, 8, 2, "standard", 10.0, "x", last_load=None),  # unanchored
               P("c", 0, 8, 2, "standard", None, "x"),                 # bad sets
               P("d", 3, 8, 2, "standard", 45.0, "x", last_load=40.0)]  # +12.5% > 10%
        codes = sorted({v.code for v in interlocks.validate_exercises(bad, max_abs_load=44.0)})
        self.assertEqual(codes, ["abs_load", "bad_prescription", "load_jump", "mode_not_allowed", "unanchored_load"])

    def test_clamp_never_exceeds_cap(self):
        self.assertEqual(interlocks.clamp_to_jump(100, 40, 10), 44.0)
        self.assertEqual(interlocks.clamp_to_jump(41, 40, 10), 41)


if __name__ == "__main__":
    unittest.main()
