"""Review round 14, batch 3: every remaining read path treats a vendor outage as an error; modes are bounded; insights say
when they are partial; old feedback is not 'recent'."""
import base64
import json
import os
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
import features  # noqa: E402
import mcp_server  # noqa: E402

BASIC = {"Authorization": "Basic " + base64.b64encode(f"gym:{TOKEN}".encode()).decode()}


class Resp:
    def __init__(self, status=200, body=None, text=""):
        self.status_code, self._body, self.text = status, body, text
        self.request = mock.Mock(headers={})

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


def make_client():
    with mock.patch.dict(os.environ, {"DATA_DIR": tempfile.mkdtemp(prefix="gym-client-")}):
        client = api_client.SpeedianceClient()
    client.save_config("42", "vendor-token", "Global", 0, "", 1, False, [], [])
    return client


READERS = {
    "template detail": (lambda c: c.get_workout_detail("ABC"), dict, {"id": 1}),
    "course detail": (lambda c: c.get_course_detail(5), dict, {"id": 5}),
    "program detail": (lambda c: c.get_program_detail(5), dict, {"id": 5}),
    "courses page": (lambda c: c.get_courses_page(1, 200), list, [{"id": 1}]),
    "programs page": (lambda c: c.get_programs_page(1, 200), list, [{"id": 1}]),
}


class EveryReadPathTreatsAnOutageAsAnError(unittest.TestCase):
    def read(self, call, resp):
        client = make_client()
        client._request = mock.Mock(return_value=resp)
        return call(client)

    def test_http_errors_and_malformed_bodies_raise(self):
        for name, (call, kind, _) in READERS.items():
            for resp in (Resp(500, {"message": "down"}), Resp(503, {"code": 0, "data": kind()}), Resp(404, {}), Resp(302, {}),
                         Resp(200, None), Resp(200, ["x"]), Resp(200, {}), Resp(200, {"code": 1001, "data": kind()}),
                         Resp(200, {"code": 0, "data": "text"})):
                with self.assertRaises(api_client.UpstreamError, msg=(name, resp.status_code, resp._body)):
                    self.read(call, resp)

    def test_401_is_unauthorized(self):
        for name, (call, _, _) in READERS.items():
            with self.assertRaises(Exception) as ctx:
                self.read(call, Resp(401, {}))
            self.assertEqual(str(ctx.exception), "Unauthorized", name)

    def test_real_answers_pass_and_an_explicit_empty_is_empty(self):
        for name, (call, kind, sample) in READERS.items():
            self.assertEqual(self.read(call, Resp(200, {"code": 0, "data": sample})), sample, name)
            self.assertEqual(self.read(call, Resp(200, {"code": 0, "data": kind()})), kind(), name)
            self.assertEqual(self.read(call, Resp(200, {"code": 0, "data": None})), kind(), name)

    def test_exercise_stats(self):
        call = lambda c: c.get_user_action_stats(5)  # noqa: E731
        for resp in (Resp(500, {"message": "down"}), Resp(302, {}), Resp(200, None, "<html>"), Resp(200, {"code": 7})):
            with self.assertRaises(api_client.UpstreamError):
                self.read(call, resp)
        body = {"code": 0, "data": {"records": [{"a": 1}]}}
        self.assertEqual(self.read(call, Resp(200, body)), body)


class RoutesAnswerWithErrorsNotEmptyPages(unittest.TestCase):
    def get(self, path, resp):
        c = gym_app.app.test_client()
        with mock.patch.object(gym_app.client, "credentials", {"token": "t", "unit": 0}), \
                mock.patch.object(gym_app.client, "_request", return_value=resp):
            return c.get(path, headers=BASIC)

    def test_an_outage_is_a_500_with_an_error_key(self):
        for path in ("/api/browse/courses", "/api/browse/programs", "/api/browse/course/5", "/api/browse/program/5",
                     "/api/workout/ABC", "/api/stats/5"):
            out = self.get(path, Resp(503, {"message": "down"}))
            self.assertEqual(out.status_code, 500, path)
            self.assertIn("error", out.get_json(), path)

    def test_healthy_answers_still_render(self):
        out = self.get("/api/browse/courses", Resp(200, {"code": 0, "data": [{"id": 1}]}))
        self.assertEqual((out.status_code, out.get_json()), (200, {"courses": [{"id": 1}]}))
        out = self.get("/api/browse/programs", Resp(200, {"code": 0, "data": []}))
        self.assertEqual((out.status_code, out.get_json()["programs"]), (200, []))

    def test_a_401_from_the_vendor_is_a_401(self):
        self.assertEqual(self.get("/api/browse/courses", Resp(401, {})).status_code, 401)


class McpWorkoutLookup(unittest.TestCase):
    def call(self, **kw):
        tool = getattr(mcp_server.get_workout, "fn", mcp_server.get_workout)
        client = mock.Mock()
        client.get_workout_detail.side_effect = kw.get("side_effect")
        client.get_workout_detail.return_value = kw.get("value")
        with mock.patch.object(mcp_server, "_client", return_value=client), mock.patch.object(mcp_server, "_require_auth"):
            return tool("ABC")

    def test_not_found_outage_and_hit_are_three_different_answers(self):
        self.assertEqual(self.call(value={})["error"], "not_found")
        self.assertEqual(self.call(side_effect=api_client.UpstreamError("template detail failed (HTTP 503)"))["error"], "request_failed")
        self.assertEqual(self.call(value={"id": 1}), {"id": 1})


class ModesAreBounded(unittest.TestCase):
    def check(self, mode):
        api_client.validate_workout("n", [{"groupId": 7, "sets": [{"reps": 5, "weight": 10, "rest": 60, "mode": mode}]}])

    def test_known_modes_and_unset_pass(self):
        for mode in (1, 2, 3, 4, 5, "3", 5.0, None, ""):
            self.check(mode)

    def test_everything_else_is_refused_without_echoing_it(self):
        for mode in (0, 6, 99, -1, 1.5, "x", True, float("nan"), float("inf"), [1], {"a": 1}):
            with self.assertRaises(ValueError, msg=repr(mode)) as ctx:
                self.check(mode)
            self.assertIn("mode", str(ctx.exception))
            self.assertNotIn("99", str(ctx.exception).replace("1 to 5", ""))


class InsightsSayWhenTheyArePartial(unittest.TestCase):
    def insights(self, **patches):
        c = gym_app.app.test_client()
        with mock.patch.object(features, "_client", mock.Mock(credentials={"token": "t"}, data_dir=tempfile.mkdtemp())), \
                mock.patch.object(features, "_authed", return_value=True), \
                mock.patch.object(features, "_history", return_value=[]), \
                mock.patch.multiple(features, **patches):
            return c.get("/api/insights", headers=BASIC).get_json()

    def session(self):
        return {"date": "2026-10-20T10:00:00", "exercises": []}

    def test_one_failed_session_is_flagged_not_ignored(self):
        calls = {"n": 0}

        def fetch(client, rec):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("boom with https://secret?token=1")
            return self.session()

        out = self.insights(_detailed_records=lambda c, d: [{"id": 1}, {"id": 2}, {"id": 3}], fetch_session_detail=fetch)
        self.assertEqual((out["sessions_failed"], out["partial"], out["detail_sessions"]), (1, True, 2))
        self.assertIn("PARTIAL", out["data_note"])
        self.assertNotIn("secret", json.dumps(out))

    def test_no_failures_means_not_partial(self):
        out = self.insights(_detailed_records=lambda c, d: [{"id": 1}], fetch_session_detail=lambda c, r: self.session())
        self.assertEqual((out["sessions_failed"], out["partial"]), (0, False))
        self.assertNotIn("PARTIAL", out["data_note"])

    def test_a_history_outage_is_a_deep_error_not_no_detail_yet(self):
        def down(c, d):
            raise api_client.UpstreamError("down https://secret?token=1")

        out = self.insights(_detailed_records=down)
        self.assertEqual(out["deep_error"], "UpstreamError")
        self.assertNotIn("detail_sessions", out)
        self.assertNotIn("secret", json.dumps(out))


class OldFeedbackIsNotRecent(unittest.TestCase):
    def profile(self, events):
        with mock.patch.object(features, "_load_feedback_events", return_value=events):
            return features._feedback_profile()

    @staticmethod
    def stamp(days_ago):
        return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def test_months_old_hard_weeks_do_not_keep_backing_the_program_off(self):
        old = [{"ts": self.stamp(120), "rpe": 9.5, "difficulty": "too_hard"}, {"ts": self.stamp(110), "rpe": 9, "difficulty": "too_hard"}]
        prof = self.profile(old)
        self.assertEqual((prof["recent_events"], prof["difficulty_signal"], prof["avg_rpe"]), (0, None, None))
        self.assertEqual(prof["events"], 2)

    def test_recent_events_still_count(self):
        prof = self.profile([{"ts": self.stamp(120), "difficulty": "too_easy"}, {"ts": self.stamp(2), "rpe": 9.5, "difficulty": "too_hard"}])
        self.assertEqual((prof["recent_events"], prof["difficulty_signal"], prof["avg_rpe"]), (1, "too_hard", 9.5))

    def test_a_pain_report_never_expires(self):
        prof = self.profile([{"ts": self.stamp(400), "pain": True, "exercise": "Cable Chest Press"},
                             {"ts": self.stamp(300), "avoid": "Lat Pulldown"}])
        self.assertEqual(prof["avoid"], ["Cable Chest Press", "Lat Pulldown"])


if __name__ == "__main__":
    unittest.main()
