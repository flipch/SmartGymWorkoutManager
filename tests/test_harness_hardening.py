"""Regression tests for issues found by an independent review: secrets parsing and tracer failure semantics."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
import synth
from harness import localsecrets, tracer


class EnvFile(unittest.TestCase):
    def load(self, text):
        d = Path(tempfile.mkdtemp())
        f = d / "creds.env"
        f.write_text(text)
        os.chmod(f, 0o600)
        return localsecrets.load_env_file(f)

    def test_one_matching_pair_of_quotes_is_removed(self):
        self.assertEqual(self.load('A="x y"\nB=\'z\'\nC=plain\n'), {"A": "x y", "B": "z", "C": "plain"})

    def test_a_password_that_starts_or_ends_with_a_quote_survives(self):
        env = self.load('P1=pa55"\nP2="pa55\nP3=\'\'quoted\'\'\nP4="\n')
        self.assertEqual(env["P1"], 'pa55"')
        self.assertEqual(env["P2"], '"pa55')
        self.assertEqual(env["P3"], "'quoted'")  # only the outer pair goes
        self.assertEqual(env["P4"], '"')

    def test_mismatched_quotes_are_left_alone(self):
        self.assertEqual(self.load("P='abc\"\n")["P"], "'abc\"")

    def test_value_may_contain_equals(self):
        self.assertEqual(self.load("P=a=b=c\n")["P"], "a=b=c")


class TracerFailureSemantics(unittest.TestCase):
    def run_with(self, history_records):
        d = Path(tempfile.mkdtemp())
        (d / "history.json").write_text(json.dumps({"records": history_records}))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = tracer.main(["--recordings", str(d), "--subjective", "3"])
        return code, out.getvalue()

    def test_drifted_history_ends_in_unavailable_not_a_traceback(self):
        rec = synth.history_record(1, synth.ts_ms(2026, 10, 20), title="Push")
        rec["startTimestamp"] = "yesterday"  # a string where a number belongs
        code, out = self.run_with([rec])
        self.assertEqual(code, 2)
        self.assertIn("unavailable", out.lower())
        self.assertNotIn("Traceback", out)

    def test_well_formed_history_still_plans(self):
        recs = [synth.history_record(i, synth.ts_ms(2026, 10, 20 - i), title="Push") for i in range(3)]
        code, _ = self.run_with(recs)
        self.assertEqual(code, 0)


class TracerBadInput(unittest.TestCase):
    def run_args(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tracer.main(["--recordings", str(Path(tempfile.mkdtemp())), *args])
        return code, out.getvalue(), err.getvalue()

    def test_bad_arguments_are_a_one_line_error_with_exit_1(self):
        cases = [("--subjective", "nan"), ("--whoop-recovery", "-5"), ("--sleep-hours", "1e308"), ("--as-of", "garbage"),
                 ("--tz", "Nowhere/City"), ("--as-of", "2026-10-26T07:00:00"), ("--readiness-file", "/nonexistent.json")]
        for args in cases:
            code, out, err = self.run_args("--subjective", "3", *args)
            self.assertEqual(code, 1, args)
            self.assertTrue(err.startswith("error: "), (args, err))
            self.assertNotIn("Traceback", err + out)
            self.assertEqual(err.count("\n"), 1, "one line")


class TracerUnit(unittest.TestCase):
    """The unit only LABELS loads; it defaults to lb (Felipe's app) and is switchable, never inferred."""

    def unit_seen(self, *args, env=None):
        import os
        from unittest import mock
        seen = []

        def fake(directory, tz, unit):
            seen.append(unit)
            return []

        base = {k: v for k, v in os.environ.items() if k != "GYM_UNIT"}
        with mock.patch.dict(os.environ, {**base, **(env or {})}, clear=True), \
                mock.patch.object(tracer, "load_recordings", side_effect=fake), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = tracer.main(["--recordings", "/x", "--subjective", "3", *args])
        return code, seen

    def test_default_is_lb(self):
        self.assertEqual(self.unit_seen()[1], ["lb"])

    def test_env_switches_it(self):
        self.assertEqual(self.unit_seen(env={"GYM_UNIT": "kg"})[1], ["kg"])

    def test_flag_beats_env(self):
        self.assertEqual(self.unit_seen("--unit", "lb", env={"GYM_UNIT": "kg"})[1], ["lb"])

    def test_bad_env_is_a_one_line_error(self):
        code, seen = self.unit_seen(env={"GYM_UNIT": "stone"})
        self.assertEqual((code, seen), (1, []))


class ReadingsDisagree(unittest.TestCase):
    def test_a_newer_reading_overriding_an_older_one_of_the_same_kind_is_reported(self):
        from datetime import datetime, timedelta
        from harness import planner
        from harness.model import Plan, Reading
        now = datetime(2026, 10, 26, 7, 0, tzinfo=synth.PT)
        rd = [Reading("subjective", 1.0, "file", now - timedelta(minutes=1)), Reading("subjective", 5.0, "manual", now)]
        plan = planner.todays_plan(sessions=[], readings=rd, now=now)
        self.assertIsInstance(plan, Plan)
        self.assertTrue(any("subjective: readings disagree" in w and "5.0 from manual" in w for w in plan.warnings), plan.warnings)
        agree = [Reading("subjective", 3.0, "file", now), Reading("subjective", 3.0, "manual", now)]
        self.assertFalse(any("disagree" in w for w in planner.todays_plan(sessions=[], readings=agree, now=now).warnings))


class DockerIgnore(unittest.TestCase):
    def test_every_secret_or_local_state_path_is_excluded_from_the_image(self):
        """Docker ignores .gitignore: without these lines `COPY . .` bakes tokens into image layers."""
        root = Path(__file__).parent.parent
        lines = {l.strip() for l in (root / ".dockerignore").read_text().splitlines() if l.strip() and not l.startswith("#")}
        for needed in (".git", ".venv", ".env", "config.json", "readiness.json", "coach_feedback.json", "recordings/",
                       "*.token.json", ".harness/", "tests/fixtures/real/", "data/"):
            self.assertIn(needed, lines, needed)

    def test_every_gitignored_secret_pattern_is_also_dockerignored(self):
        root = Path(__file__).parent.parent
        git = {l.strip() for l in (root / ".gitignore").read_text().splitlines() if l.strip() and not l.startswith("#")}
        docker = {l.strip() for l in (root / ".dockerignore").read_text().splitlines() if l.strip() and not l.startswith("#")}
        secretish = {g for g in git if any(w in g for w in ("env", "token", "config", "readiness", "recordings", "harness",
                                                             "real", "feedback", "data"))}
        missing = sorted(g for g in secretish if g.rstrip("/") not in {d.rstrip("/") for d in docker})
        self.assertEqual(missing, [], "gitignored secret/state patterns missing from .dockerignore")


if __name__ == "__main__":
    unittest.main()
