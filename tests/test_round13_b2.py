"""Review round 13, batch 2: avoided exercises stay avoided, history reads fail loudly, WHOOP recovery is the newest
cycle's, stale readings are announced, the coach Save button saves, timed sets are saved as seconds, --port is real."""
import base64
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

TOKEN = "t" * 32
os.environ["GYM_API_TOKEN"] = TOKEN
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="gym-test-data-")
os.environ.pop("GYM_AUTH_DISABLED", None)
os.environ.pop("GYM_ALLOW_WRITES", None)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import api_client  # noqa: E402
import app as gym_app  # noqa: E402
import coach  # noqa: E402
import features  # noqa: E402
from harness import planner, whoop, whoop_client as wc  # noqa: E402
from harness.errors import SchemaDrift  # noqa: E402
from harness.model import Reading  # noqa: E402

BASIC = {"Authorization": "Basic " + base64.b64encode(f"gym:{TOKEN}".encode()).decode()}
NODE = shutil.which("node")
ROOT = os.path.join(os.path.dirname(__file__), "..")


def names(program):
    return [e["name"] for d in program["days"] for e in d["exercises"]]


class AvoidedExercisesStayAvoided(unittest.TestCase):
    def test_the_sole_pick_of_a_day_is_not_restored(self):
        """Pain report on the Pull day's only exercise: the old `or picks` fallback prescribed it anyway."""
        base = coach.generate_program(goal="general", days_per_week=5, experience="beginner")
        for victim in set(names(base)):
            prog = coach.generate_program(goal="general", days_per_week=5, experience="beginner",
                                          feedback={"avoid": [victim]})
            self.assertNotIn(victim.lower(), [n.lower() for n in names(prog)], victim)

    def test_across_goals_experience_and_split(self):
        for goal, exp, days in itertools.product(("muscle", "strength", "fatloss", "general"),
                                                 ("beginner", "intermediate", "advanced"), (2, 4)):
            base = coach.generate_program(goal=goal, days_per_week=days, experience=exp)
            avoid = sorted(set(names(base)))[:3]
            prog = coach.generate_program(goal=goal, days_per_week=days, experience=exp, feedback={"avoid": avoid})
            lowered = {n.lower() for n in names(prog)}
            for a in avoid:
                self.assertNotIn(a.lower(), lowered, (goal, exp, days, a))

    def test_case_is_ignored_and_substitutes_are_chosen_when_possible(self):
        base = coach.generate_program(goal="muscle", days_per_week=4, experience="intermediate")
        victim = names(base)[0]
        prog = coach.generate_program(goal="muscle", days_per_week=4, experience="intermediate",
                                      feedback={"avoid": [victim.upper()]})
        self.assertNotIn(victim.lower(), [n.lower() for n in names(prog)])
        self.assertGreaterEqual(len(names(prog)), len(names(base)) - 2)  # a substitute, not a gaping hole

    def test_avoiding_everything_gives_empty_days_not_the_excluded_lifts(self):
        everything = [e["name"] for e in coach.CATALOG]
        prog = coach.generate_program(goal="general", days_per_week=3, experience="beginner", feedback={"avoid": everything})
        self.assertEqual(names(prog), [])


class UnilateralSummaryUsesTheMachineTotal(unittest.TestCase):
    """Real shape from a Barbell Bulgarian Split Squat set: weights 74 (both cables), left/right 37 each, capacity 888/12 reps."""

    def row(self, side, **info):
        return {"leftRight": side, "finishedCount": 12, "targetCount": 12, "capacity": 888, "trainingInfoDetail": info}

    def test_total_first_so_the_web_matches_the_editor_and_the_harness(self):
        from harness.normalize import set_load
        row = self.row(1, weights=[74.0] * 12, leftWeights=[37.0] * 12, rightWeights=[37.0] * 12)
        self.assertEqual(features._summarize_set(row, True)["weight"], 74.0)
        self.assertEqual(features._summarize_set(row, True)["weight"], set_load(row))
        self.assertEqual(features._summarize_set(self.row(2, **row["trainingInfoDetail"]), True)["weight"], 74.0)

    def test_the_trained_sides_array_is_only_a_fallback(self):
        self.assertEqual(features._summarize_set(self.row(1, leftWeights=[37.0] * 12), True)["weight"], 37.0)
        self.assertEqual(features._summarize_set(self.row(2, rightWeights=[36.0] * 12), True)["weight"], 36.0)
        self.assertEqual(features._summarize_set(self.row(1, leftWeights=[], weights=[]), True)["weight"], 74.0)  # capacity/reps

    def test_the_side_label_is_kept(self):
        self.assertEqual(features._summarize_set(self.row(1, weights=[74.0]), True)["side"], "L")
        self.assertEqual(features._summarize_set(self.row(2, weights=[74.0]), True)["side"], "R")

    def test_a_bilateral_set_is_unchanged(self):
        self.assertEqual(features._summarize_set(self.row(0, weights=[40.0] * 12), False)["weight"], 40.0)

    def test_a_mid_set_change_is_still_summarised(self):
        out = features._summarize_set(self.row(1, weights=[74.0] * 8 + [60.0] * 4), True)
        self.assertEqual((out["weight"], out["weight_note"]), ([74.0, 60.0], "8x74.0, 4x60.0"))


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body, self.text = status, body, ""

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


def make_client():
    with mock.patch.dict(os.environ, {"DATA_DIR": tempfile.mkdtemp(prefix="gym-client-")}):
        client = api_client.SpeedianceClient()
    client.save_config("42", "vendor-token", "Global", 0, "", 1, False, [], [])
    return client


class HistoryReadsFailLoudly(unittest.TestCase):
    CALLS = (lambda c: c.get_training_history("2026-10-01", "2026-10-26"),
             lambda c: c.get_training_records("2026-10-01", "2026-10-26"),
             lambda c: c.get_training_stats("2026-10-01", "2026-10-26"),
             lambda c: c.get_training_detail("123", "custom"))

    def run_call(self, call, resp):
        client = make_client()
        client._request = mock.Mock(return_value=resp)
        return call(client)

    def test_http_errors_with_a_json_body_are_errors_not_empty(self):
        for call in self.CALLS:
            for status in (400, 403, 404, 429, 500, 502, 503):
                with self.assertRaises(api_client.UpstreamError, msg=status):
                    self.run_call(call, Resp(status, {"message": "down"}))

    def test_a_non_200_is_an_error_even_with_a_success_looking_body(self):
        """A gateway can answer 502/503 with a cached or templated body: only the status says it failed."""
        for call in self.CALLS:
            for status in (201, 204, 301, 404, 500, 502, 503):
                with self.assertRaises(api_client.UpstreamError, msg=status):
                    self.run_call(call, Resp(status, {"code": 0, "data": [] if call is not self.CALLS[2] else {}}))

    def test_a_401_stays_unauthorized(self):
        for call in self.CALLS:
            with self.assertRaises(Exception) as ctx:
                self.run_call(call, Resp(401, {"message": "no"}))
            self.assertEqual(str(ctx.exception), "Unauthorized")

    def test_rejections_and_malformed_bodies_are_errors(self):
        for call in self.CALLS:
            for resp in (Resp(200, {"code": 1001, "data": []}), Resp(200, None), Resp(200, ["x"]), Resp(200, {}),
                         Resp(200, {"message": "x"}), Resp(200, {"data": "text"})):
                with self.assertRaises(api_client.UpstreamError):
                    self.run_call(call, resp)

    def test_real_answers_pass_including_a_genuinely_empty_history(self):
        hist = self.CALLS[0]
        self.assertEqual(self.run_call(hist, Resp(200, {"code": 0, "data": [{"trainingId": 1}]})), [{"trainingId": 1}])
        self.assertEqual(self.run_call(hist, Resp(200, {"code": 0, "data": []})), [])
        self.assertEqual(self.run_call(hist, Resp(200, {"code": 0})), [])
        self.assertEqual(self.run_call(hist, Resp(200, {"code": 0, "data": None})), [])
        self.assertEqual(self.run_call(self.CALLS[2], Resp(200, {"code": 0, "data": {"a": 1}})), {"a": 1})
        self.assertEqual(self.run_call(self.CALLS[3], Resp(200, {"code": 0, "data": [{"x": 1}]})), [{"x": 1}])
        self.assertEqual(self.run_call(self.CALLS[3], Resp(200, {"code": 0, "data": {"x": 1}})), {"x": 1})

    def test_the_recovery_endpoint_says_unavailable_not_well_rested(self):
        c = gym_app.app.test_client()
        for resp in (Resp(503, {"message": "down"}), Resp(401, {"message": "no"}), Resp(200, {"message": "oops"})):
            with mock.patch.object(gym_app.client, "credentials", {"token": "vendor-token", "unit": 0}), \
                    mock.patch.object(gym_app.client, "_request", return_value=resp):
                out = c.get("/api/recovery", headers=BASIC)
            self.assertNotEqual(out.status_code, 200, resp.status_code)
            self.assertNotIn("Well rested", out.get_data(as_text=True))


def rec(cycle, score, created, state="SCORED"):
    r = {"cycle_id": cycle, "sleep_id": "11111111-1111-1111-1111-111111111111", "user_id": 7,
         "created_at": created, "updated_at": created, "score_state": state}
    if state == "SCORED":
        r["score"] = {"user_calibrating": False, "recovery_score": score, "resting_heart_rate": 52.0, "hrv_rmssd_milli": 65.0}
    return r


class WhoopRecoveryIsTheNewestCycles(unittest.TestCase):
    def test_a_backfilled_older_cycle_does_not_beat_todays(self):
        today = rec(200, 10.0, "2026-10-26T14:00:00Z")
        backfilled = rec(100, 90.0, "2026-10-26T15:00:00Z")  # written later, but it is yesterday's cycle
        for order in ([today, backfilled], [backfilled, today]):
            readings, _ = whoop.parse_recovery({"records": order, "next_token": None})
            self.assertEqual(readings[0].value, 10.0)
            self.assertEqual(readings[0].as_of, datetime(2026, 10, 26, 14, 0, tzinfo=timezone.utc))

    def test_an_unscored_newest_cycle_is_not_replaced_by_an_older_score(self):
        readings, notes = whoop.parse_recovery({"records": [rec(100, 90.0, "2026-10-25T14:00:00Z"),
                                                            rec(200, None, "2026-10-26T14:00:00Z", state="PENDING_SCORE")],
                                                "next_token": None})
        self.assertEqual(readings, [])
        self.assertIn("PENDING_SCORE", notes[0])

    def test_the_same_cycle_twice_takes_the_later_write(self):
        readings, _ = whoop.parse_recovery({"records": [rec(5, 40.0, "2026-10-26T10:00:00Z"),
                                                        rec(5, 60.0, "2026-10-26T12:00:00Z")], "next_token": None})
        self.assertEqual(readings[0].value, 60.0)

    def test_a_non_integer_cycle_id_is_schema_drift(self):
        for bad in ("200", 2.5, None, True, [1]):
            r = rec(1, 50.0, "2026-10-26T14:00:00Z")
            r["cycle_id"] = bad
            with self.assertRaises(SchemaDrift, msg=repr(bad)):
                whoop.parse_recovery({"records": [r], "next_token": None})


class StaleReadingsAreAnnounced(unittest.TestCase):
    NOW = datetime(2026, 10, 26, 21, 0, tzinfo=timezone.utc)

    def plan(self, readings):
        import synth
        from harness.normalize import session_header
        day = self.NOW.date() - timedelta(days=2)
        rec_ = synth.history_record(1, synth.ts_ms(day.year, day.month, day.day), title="Push")
        return planner.todays_plan(sessions=[session_header(rec_)], readings=readings, now=self.NOW, tz="UTC")

    def test_a_dropped_bad_recovery_is_named_in_the_warnings(self):
        stale = Reading("whoop_recovery", 15.0, "whoop", self.NOW - timedelta(hours=19))
        fresh = Reading("subjective", 5.0, "manual", self.NOW - timedelta(minutes=5))
        plan = self.plan([stale, fresh])
        joined = " | ".join(plan.warnings)
        self.assertIn("ignored stale whoop_recovery 15", joined)
        self.assertIn("older than 18 h", joined)

    def test_no_stale_reading_no_such_warning(self):
        fresh = Reading("subjective", 5.0, "manual", self.NOW - timedelta(minutes=5))
        self.assertNotIn("ignored stale", " | ".join(self.plan([fresh]).warnings))


@unittest.skipUnless(NODE, "node is required")
class CoachSaveButtonSaves(unittest.TestCase):
    HARNESS = r"""
const vm = require('vm');
const els = {};
const el = id => (els[id] ??= {textContent: '', innerHTML: '', value: ''});
globalThis.document = {getElementById: el};
globalThis.window = {__P: {days: [{name: 'd', exercises: []}]}};
let sent = null;
globalThis.fetch = async (u, o) => { sent = {url: u, body: JSON.parse(o.body)}; return {status: 200, json: async () => ({results: [{created: true, name: 'n', exercise_count: 1}]})}; };
vm.runInThisContext(%(script)s);
(async () => { await saveDay(0); console.log(JSON.stringify(sent)); })();
"""

    def test_the_button_sends_an_explicit_real_save(self):
        resp = gym_app.app.test_client().get("/coach", headers=BASIC)
        script = re.search(r"<script>(.*?)</script>", resp.get_data(as_text=True), re.S).group(1)
        out = subprocess.run([NODE, "-e", self.HARNESS % {"script": json.dumps(script)}], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        sent = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual(sent["url"], "/api/coach/apply")
        self.assertIs(sent["body"]["dry_run"], False)  # a literal false: the endpoint treats anything else as a preview
        self.assertEqual(sent["body"]["day_index"], 0)


class EditorSavesTheUnitItShows(unittest.TestCase):
    def test_the_save_path_uses_the_shared_effective_unit(self):
        html = gym_app.app.test_client().get("/create", headers=BASIC)
        self.assertIn(html.status_code, (200, 302))
        src = open(os.path.join(ROOT, "templates", "create.html"), encoding="utf-8").read()
        save = src[src.index("window.saveWorkout"):]
        save = save[:save.index("await fetch('/create'")]
        self.assertIn("WorkoutLogic.effectiveSetUnit(ex, s)", save)
        self.assertIn("newSet.unit = shown", save)
        self.assertIn("return WorkoutLogic.getSetGoalUnit(ex)", src)  # the display uses the same rule


@unittest.skipUnless(NODE, "node is required")
class JavaScriptUnitTestsAreInTheGate(unittest.TestCase):
    def test_workout_logic_node_tests_pass(self):
        out = subprocess.run([NODE, "--test", os.path.join(ROOT, "tests", "workout-logic.test.mjs")],
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout[-1500:] + out.stderr[-500:])


class OAuthPortIsReal(unittest.TestCase):
    def test_redirect_uri_follows_the_port(self):
        self.assertEqual(wc.redirect_uri_for(8000), wc.REDIRECT_URI)
        self.assertEqual(wc.redirect_uri_for(8765), "http://localhost:8765/callback")
        creds = wc.AppCredentials("cid", "secret")
        self.assertIn("redirect_uri=http%3A%2F%2Flocalhost%3A8765%2Fcallback", wc.authorize_url(creds, "s", wc.redirect_uri_for(8765)))
        self.assertIn("localhost%3A8000", wc.authorize_url(creds, "s"))  # default unchanged

    def test_consent_uses_the_same_port_for_the_authorize_url_and_the_token_exchange(self):
        import socket
        import threading
        import urllib.request
        from urllib.parse import parse_qs, urlparse
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        seen = {}

        class Http:
            def post(self, url, data=None, **kw):
                seen["exchange"] = data["redirect_uri"]
                return Resp(200, {"access_token": "a", "refresh_token": "r", "expires_in": 3600,
                                  "scope": "offline read:recovery read:sleep", "token_type": "bearer"})

        class Store:
            path = "unused"

            def save(self, tokens):
                seen["saved"] = True

        def open_url(url):
            q = parse_qs(urlparse(url).query)
            seen["authorize"] = q["redirect_uri"][0]
            threading.Thread(target=lambda: urllib.request.urlopen(
                f"http://127.0.0.1:{port}/callback?code=C&state={q['state'][0]}", timeout=5).read(), daemon=True).start()

        wc.run_consent(wc.AppCredentials("cid", "secret"), Store(), open_url=open_url, port=port, timeout_s=5, http=Http())
        self.assertEqual(seen["authorize"], f"http://localhost:{port}/callback")
        self.assertEqual(seen["exchange"], seen["authorize"])
        self.assertTrue(seen["saved"])


if __name__ == "__main__":
    unittest.main()
