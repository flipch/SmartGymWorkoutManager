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


if __name__ == "__main__":
    unittest.main()
