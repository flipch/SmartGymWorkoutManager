"""Asking the gym MCP for the WHOOP recovery score must never come back blank.

Seen in ChatGPT: autoregulate() with no arguments failed with "readiness has no signal" although a fresh WHOOP score was
stored (the empty argument set was checked before the stored readings were merged in), and recovery_recommendation() never
mentioned WHOOP at all.
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import test_round16  # noqa: F401  (sets the shared secret and imports the app the way the other tests do)
import features
import mcp_server


def tool(name):
    t = getattr(mcp_server, name)
    return getattr(t, "fn", t)


class Stored(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="gym-blank-")
        self.client = mock.Mock(data_dir=self.dir)
        self.patches = [mock.patch.object(mcp_server, "_client", return_value=self.client),
                        mock.patch.object(mcp_server, "_require_auth"),
                        mock.patch.object(mcp_server, "_pull_whoop_if_due", return_value=None)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)


class AutoregulateWithoutArguments(Stored):
    def test_it_uses_the_stored_whoop_score(self):
        features.save_readiness({"whoop_recovery": 20, "source": "whoop"}, self.dir)
        out = tool("autoregulate")()
        self.assertNotIn("error", out)
        self.assertEqual(out["band"], "low")  # the stored 20 was used, not a blank

    def test_a_high_stored_score_gives_a_band_too(self):
        features.save_readiness({"whoop_recovery": 81, "source": "whoop"}, self.dir)
        out = tool("autoregulate")()
        self.assertNotIn("error", out)
        self.assertIn("band", out)

    def test_with_nothing_stored_the_error_says_what_to_do(self):
        out = tool("autoregulate")()
        self.assertEqual(out["error"], "request_failed")
        self.assertIn("none was given and no fresh one is stored", out["message"])
        self.assertIn("WHOOP:", out["message"])

    def test_a_pure_what_if_with_no_arguments_still_asks_for_a_signal(self):
        features.save_readiness({"whoop_recovery": 20, "source": "whoop"}, self.dir)
        out = tool("autoregulate")(use_stored_readiness=False)
        self.assertIn("give at least one of", out["message"])  # the stored score must not leak into a what-if

    def test_an_out_of_range_reading_is_still_refused(self):
        self.assertIn("error", tool("autoregulate")(whoop_recovery=1000.0))


class RecoveryRecommendationShowsWhoop(Stored):
    def run_tool(self):
        self.client.get_training_history.return_value = []
        return tool("recovery_recommendation")()

    def test_the_stored_whoop_score_and_band_come_back(self):
        features.save_readiness({"whoop_recovery": 20, "sleep_hours": 8, "source": "whoop"}, self.dir)
        out = self.run_tool()
        self.assertEqual(out["readiness"]["signals"]["whoop_recovery"], 20)
        self.assertEqual(out["readiness"]["band"], "low")
        self.assertIn("recommendation", out)

    def test_without_a_stored_reading_it_says_where_to_look(self):
        out = self.run_tool()
        self.assertIsNone(out["readiness"])
        self.assertIn("get_readiness", out["readiness_hint"])

    def test_an_unreadable_readiness_never_breaks_the_recommendation(self):
        with mock.patch.object(mcp_server, "_fresh_readiness", side_effect=OSError("disk")):
            out = self.run_tool()
        self.assertIn("recommendation", out)
        self.assertIsNone(out["readiness"])


if __name__ == "__main__":
    unittest.main()
