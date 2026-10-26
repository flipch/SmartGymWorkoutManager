"""Training-frequency maths, outage semantics and input validation (review round 12: history correctness)."""
import base64
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from unittest import mock

TOKEN = "t" * 32
os.environ["GYM_API_TOKEN"] = TOKEN
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="gym-test-data-")
os.environ.pop("GYM_AUTH_DISABLED", None)
os.environ.pop("GYM_ALLOW_WRITES", None)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import api_client  # noqa: E402
import app as gym_app  # noqa: E402
import features  # noqa: E402
import insights_core as ic  # noqa: E402

BASIC = {"Authorization": "Basic " + base64.b64encode(f"gym:{TOKEN}".encode()).decode()}
TODAY = date(2026, 10, 26)


def ago(n):
    return TODAY - timedelta(days=n)


class Windows(unittest.TestCase):
    def test_seven_day_window_is_seven_calendar_days_including_today(self):
        self.assertEqual(ic.in_last([ago(6)], TODAY, 7), 1)   # today-6 is inside
        self.assertEqual(ic.in_last([ago(7)], TODAY, 7), 0)   # today-7 is the 8th day: outside

    def test_thirty_day_window_boundary(self):
        self.assertEqual(ic.in_last([ago(29)], TODAY, 30), 1)
        self.assertEqual(ic.in_last([ago(30)], TODAY, 30), 0)

    def test_future_dated_records_are_ignored_everywhere(self):
        future = [TODAY + timedelta(days=1), TODAY + timedelta(days=400)]
        self.assertEqual(ic.in_last(future, TODAY, 7), 0)
        s = ic.summarize(future, TODAY)
        self.assertEqual((s["last_session"], s["active_days"], s["current_streak_days"]), (None, 0, 0))
        self.assertEqual(ic.recovery_advice(future, TODAY)["days_since_last"], None)

    def test_duplicate_days_count_once(self):
        self.assertEqual(ic.in_last([ago(1), ago(1), ago(1)], TODAY, 7), 1)

    def test_non_dates_are_ignored(self):
        self.assertEqual(ic.in_last([None, "2026-10-25", 5, ago(1)], TODAY, 7), 1)


class Summary(unittest.TestCase):
    def test_streak_counts_back_from_today(self):
        self.assertEqual(ic.summarize([TODAY, ago(1), ago(2), ago(4)], TODAY)["current_streak_days"], 3)

    def test_streak_survives_a_day_not_yet_trained(self):
        self.assertEqual(ic.summarize([ago(1), ago(2)], TODAY)["current_streak_days"], 2)

    def test_streak_broken_by_a_gap(self):
        self.assertEqual(ic.summarize([ago(2), ago(3)], TODAY)["current_streak_days"], 0)

    def test_average_per_week(self):
        dates = [ago(i) for i in range(0, 30, 3)]  # 10 sessions in 30 days
        self.assertEqual(ic.summarize(dates, TODAY)["avg_sessions_per_week"], 2.3)

    def test_empty(self):
        s = ic.summarize([], TODAY)
        self.assertEqual((s["sessions_last_7d"], s["last_session"], s["avg_sessions_per_week"]), (0, None, 0))


class Recovery(unittest.TestCase):
    def test_no_history_is_train_but_flagged_unknown_days_since(self):
        r = ic.recovery_advice([], TODAY)
        self.assertEqual((r["recommendation"], r["days_since_last"]), ("train", None))

    def test_six_sessions_in_seven_days_is_rest(self):
        r = ic.recovery_advice([ago(i) for i in range(1, 7)], TODAY)
        self.assertEqual((r["recommendation"], r["sessions_last_7d"]), ("rest", 6))

    def test_five_sessions_is_not_rest(self):
        self.assertEqual(ic.recovery_advice([ago(i) for i in range(1, 6)], TODAY)["recommendation"], "train")

    def test_already_trained_today_with_two_sessions_is_active_recovery(self):
        self.assertEqual(ic.recovery_advice([TODAY, ago(1)], TODAY)["recommendation"], "active_recovery")

    def test_three_days_off_is_well_rested(self):
        r = ic.recovery_advice([ago(3)], TODAY)
        self.assertEqual((r["recommendation"], r["days_since_last"]), ("train", 3))
        self.assertIn("rested", r["message"])

    def test_two_days_off_is_not_the_well_rested_message(self):
        self.assertNotIn("rested", ic.recovery_advice([ago(2)], TODAY)["message"])


class Clamps(unittest.TestCase):
    def test_days(self):
        for value, want in ((0, 1), (-5, 1), (10**9, ic.MAX_DAYS), ("12", 12), ("x", 30), (None, 30), (True, 30),
                            (float("inf"), 30), (7, 7)):
            self.assertEqual(ic.clamp_days(value, 30), want, repr(value))

    def test_n(self):
        for value, want in ((0, 1), (-1, 1), (10**6, ic.MAX_N), ("3", 3), ("x", 1), (None, 1), (False, 1)):
            self.assertEqual(ic.clamp_n(value, 1), want, repr(value))

    def test_record_id(self):
        for good in ("123", "abc-DEF_9"):
            self.assertTrue(ic.valid_record_id(good), good)
        for bad in ("", "../x", "a/b", "a?b=1", "a b", "1" * 65, "a\n", None, 5, "a.b", "%2e"):
            self.assertFalse(ic.valid_record_id(bad), repr(bad))

    def test_iso_date(self):
        self.assertTrue(ic.valid_iso_date("2026-10-26"))
        for bad in ("2026-13-01", "26-10-2026", "2026-10-26&x=1", "", None, "2026-1-1", "20261026"):
            self.assertFalse(ic.valid_iso_date(bad), repr(bad))


class OutageIsNotRest(unittest.TestCase):
    """A vendor outage must read as an error, never as 'no training -> well rested'."""

    def setUp(self):
        self.c = gym_app.app.test_client()
        fake = mock.Mock()
        fake.credentials = {"token": "x"}
        fake.get_training_history.side_effect = RuntimeError("secret-token-in-url?token=abc")
        self.patch = mock.patch.object(features, "_client", fake)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_recovery_endpoint_returns_503_and_no_recommendation(self):
        resp = self.c.get("/api/recovery", headers=BASIC)
        body = resp.get_json()
        self.assertEqual((resp.status_code, body["error"]), (503, "history_unavailable"))
        self.assertNotIn("recommendation", body)
        self.assertNotIn("secret-token", resp.get_data(as_text=True))

    def test_insights_endpoint_returns_503(self):
        resp = self.c.get("/api/insights", headers=BASIC)
        self.assertEqual((resp.status_code, resp.get_json()["error"]), (503, "history_unavailable"))

    def test_history_helper_raises_only_a_type_name(self):
        with self.assertRaises(features.HistoryUnavailable) as ctx:
            features._history(30)
        self.assertEqual(str(ctx.exception), "RuntimeError")
        self.assertIsNone(ctx.exception.__cause__)

    def test_signed_out_is_still_an_empty_history_not_an_error(self):
        with mock.patch.object(features, "_client", mock.Mock(credentials={})):
            self.assertEqual(features._history(30), [])

    def test_workout_history_api_does_not_leak_exception_text(self):
        with mock.patch.object(gym_app, "client", features._client):
            resp = self.c.get("/api/workout_history?start=2026-10-01&end=2026-10-26", headers=BASIC)
        self.assertEqual(resp.status_code, 502)
        self.assertNotIn("secret-token", resp.get_data(as_text=True))


class AppInputValidation(unittest.TestCase):
    def setUp(self):
        self.c = gym_app.app.test_client()
        self.client = mock.Mock()
        self.client.credentials = {"token": "x"}
        self.client.base_url = "https://api2.speediance.com"
        self.client._get_headers.return_value = {}
        p = mock.patch.object(gym_app, "client", self.client)
        p.start()
        self.addCleanup(p.stop)

    def test_detail_rejects_path_tricks_before_any_upstream_call(self):
        with mock.patch.object(gym_app.requests, "get") as upstream:
            for rid in ("..%2f..%2fsecret", "a%3Fb%3D1", "a.b", "x" * 80):
                resp = self.c.get(f"/api/workout_history/detail/{rid}", headers=BASIC)
                self.assertEqual(resp.status_code, 400, rid)
            upstream.assert_not_called()

    def test_detail_error_does_not_echo_the_upstream_url(self):
        with mock.patch.object(gym_app.requests, "get", return_value=mock.Mock(status_code=404)):
            resp = self.c.get("/api/workout_history/detail/123", headers=BASIC)
        self.assertEqual(resp.status_code, 404)
        self.assertNotIn("url", resp.get_json())
        self.assertNotIn("speediance", resp.get_data(as_text=True))

    def test_history_dates_must_be_iso(self):
        for start in ("x", "2026-10-01%26admin%3D1", "2026-13-40"):
            resp = self.c.get(f"/api/workout_history?start={start}&end=2026-10-26", headers=BASIC)
            self.assertEqual(resp.status_code, 400, start)
        self.client.get_training_history.assert_not_called()


class UpstreamUrlsAreEncoded(unittest.TestCase):
    def setUp(self):
        self.client = api_client.SpeedianceClient.__new__(api_client.SpeedianceClient)
        self.client.base_url = "https://api2.speediance.com"
        self.client.device_type = 1
        self.urls = []

        def fake_request(method, url, **kw):
            self.urls.append(url)
            return mock.Mock(json=lambda: {"data": {}}, status_code=200)

        self.client._request = fake_request
        self.client._get_headers = lambda: {}

    def test_training_detail_id_cannot_escape_its_path_segment(self):
        try:
            self.client.get_training_detail("1/../../admin?x=1", "custom")
        except Exception:
            pass
        self.assertTrue(self.urls)
        for url in self.urls:
            self.assertNotIn("/../", url)
            self.assertNotIn("?x=1", url)
            self.assertIn("1%2F..%2F..%2Fadmin%3Fx%3D1", url)

    def test_history_dates_are_validated(self):
        with self.assertRaises(ValueError):
            api_client._d("2026-10-01&endDate=1")
        self.assertEqual(api_client._d("2026-10-01"), "2026-10-01")

    def test_template_ids_keep_their_commas(self):
        self.assertEqual(api_client._q("1,2,3", ","), "1,2,3")
        self.assertEqual(api_client._q("a&b"), "a%26b")


class ClientReadsDoNotSwallowOutages(unittest.TestCase):
    """A failed read must raise; an empty list/dict reads as 'no workouts' / 'workout with no sets'."""

    def setUp(self):
        self.client = api_client.SpeedianceClient.__new__(api_client.SpeedianceClient)
        self.client.base_url = "https://api2.speediance.com"
        self.client._get_headers = lambda: {}
        self.urls = []

        def boom(method, url, **kw):
            self.urls.append((method, url))
            raise ConnectionError("net down https://api2.speediance.com/secret")

        self.client._request = boom

    def test_each_read_raises(self):
        reads = (lambda: self.client.get_training_history("2026-10-01", "2026-10-26"),
                 lambda: self.client.get_training_records("2026-10-01", "2026-10-26"),
                 lambda: self.client.get_training_stats("2026-10-01", "2026-10-26"),
                 lambda: self.client.get_training_detail(1, "custom"))
        for i, read in enumerate(reads):
            with self.assertRaises(ConnectionError, msg=str(i)):
                read()

    def test_unauthorized_still_surfaces_as_unauthorized(self):
        self.client._request = lambda *a, **k: mock.Mock(status_code=401)
        with self.assertRaises(Exception) as ctx:
            self.client.get_training_records("2026-10-01", "2026-10-26")
        self.assertEqual(str(ctx.exception), "Unauthorized")

    def test_log_line_has_no_exception_text(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(ConnectionError):
            self.client.get_training_records("2026-10-01", "2026-10-26")
        self.assertIn("ConnectionError", buf.getvalue())
        self.assertNotIn("secret", buf.getvalue())

    def test_only_one_training_detail_definition(self):
        import inspect
        src = inspect.getsource(api_client)
        self.assertEqual(src.count("def get_training_detail("), 1)


class UrlEncodingPerEndpoint(unittest.TestCase):
    def setUp(self):
        self.client = api_client.SpeedianceClient.__new__(api_client.SpeedianceClient)
        self.client.base_url = "https://api2.speediance.com"
        self.client._get_headers = lambda: {}
        self.urls = []
        self.client._request = lambda method, url, **kw: (self.urls.append(url),
                                                           mock.Mock(json=lambda: {"data": {}}, status_code=200))[1]

    def test_course_detail_id_is_quoted(self):
        self.client.get_training_detail("1/../x", "course")
        self.assertIn("courseTrainingInfoDetail/1%2F..%2Fx", self.urls[0])

    def test_custom_detail_id_is_quoted(self):
        self.client.get_training_detail("1?y=1", "custom")
        self.assertIn("cttTrainingInfoDetail/1%3Fy%3D1", self.urls[0])

    def test_delete_keeps_the_comma_separated_id_list(self):
        self.client.delete_workout("11,22,33")
        self.assertTrue(self.urls[0].endswith("customTrainingTemplate?ids=11,22,33"), self.urls[0])

    def test_delete_cannot_smuggle_a_second_query_parameter(self):
        self.client.delete_workout("11&admin=1")
        self.assertNotIn("&admin=1", self.urls[0])


class RecoveryEdge(unittest.TestCase):
    def test_a_single_session_today_is_not_active_recovery(self):
        r = ic.recovery_advice([TODAY], TODAY)
        self.assertEqual(r["recommendation"], "train")
        self.assertEqual(r["message"], "Good to train today. ✅")


class ErrorBodiesCarryNoUpstreamText(unittest.TestCase):
    def setUp(self):
        self.c = gym_app.app.test_client()
        fake = mock.Mock()
        fake.credentials = {"token": "x"}
        fake.base_url = "https://api2.speediance.com"
        for name in ("get_training_records", "get_training_stats", "get_training_detail",
                     "get_training_session_info", "get_training_history"):
            getattr(fake, name).side_effect = ConnectionError("leak https://api2.speediance.com/x?token=abc")
        self.fake = fake
        for target in (gym_app, features):
            attr = "client" if target is gym_app else "_client"
            p = mock.patch.object(target, attr, fake)
            p.start()
            self.addCleanup(p.stop)

    def _check(self, path, status):
        resp = self.c.get(path, headers=BASIC)
        self.assertEqual(resp.status_code, status, path)
        text = resp.get_data(as_text=True)
        self.assertIn("ConnectionError", text, path)
        self.assertNotIn("token=abc", text, path)
        self.assertNotIn("speediance.com", text, path)

    def test_api_history(self):
        self._check("/api/history?start=2026-10-01&end=2026-10-26", 502)

    def test_api_history_detail(self):
        self._check("/api/history/detail/7", 502)

    def test_api_last_workout(self):
        self._check("/api/last_workout", 502)

    def test_coach_critique_by_code(self):
        self.fake.get_workout_detail.side_effect = ConnectionError("leak https://api2.speediance.com/x?token=abc")
        resp = self.c.post("/api/coach/critique", headers=BASIC, json={"code": "abc"})
        text = resp.get_data(as_text=True)
        self.assertEqual(resp.status_code, 502)
        self.assertIn("ConnectionError", text)
        self.assertNotIn("token=abc", text)


class McpHealthyPaths(unittest.TestCase):
    def test_recovery_recommendation_uses_the_history(self):
        import mcp_server
        today = date.today()
        recs = [{"startTimestamp": int((__import__("datetime").datetime.combine(today - timedelta(days=i),
                                                                                  __import__("datetime").time(12))
                                       ).timestamp() * 1000)} for i in range(1, 7)]
        fake = mock.Mock(credentials={"token": "x"})
        fake.get_training_history.return_value = recs
        with mock.patch.object(mcp_server, "_client", lambda: fake):
            out = getattr(mcp_server.recovery_recommendation, "fn", mcp_server.recovery_recommendation)()
        self.assertEqual((out["recommendation"], out["sessions_last_7d"], out["days_since_last"]), ("rest", 6, 1))

    def test_workout_insights_counts_sessions(self):
        import mcp_server
        today = date.today()
        ts = int(__import__("datetime").datetime.combine(today, __import__("datetime").time(9)).timestamp() * 1000)
        fake = mock.Mock(credentials={"token": "x"})
        fake.get_training_history.return_value = [{"startTimestamp": ts}]
        with mock.patch.object(mcp_server, "_client", lambda: fake):
            out = getattr(mcp_server.workout_insights, "fn", mcp_server.workout_insights)()
        self.assertEqual((out["total_sessions"], out["sessions_last_7d"], out["current_streak_days"]), (1, 1, 1))


class McpToolsOnOutage(unittest.TestCase):
    """The MCP tools used to compute 'well rested' from a swallowed outage."""

    def setUp(self):
        import mcp_server
        self.m = mcp_server
        fake = mock.Mock()
        fake.credentials = {"token": "x"}
        fake.get_training_history.side_effect = ConnectionError("down")
        p = mock.patch.object(mcp_server, "_client", lambda: fake)
        p.start()
        self.addCleanup(p.stop)

    @staticmethod
    def raw(tool):
        return getattr(tool, "fn", tool)

    def test_recovery_recommendation_is_an_error_not_train(self):
        out = self.raw(self.m.recovery_recommendation)()
        self.assertEqual(out["error"], "request_failed")
        self.assertNotIn("recommendation", out)

    def test_workout_insights_is_an_error_not_zero_sessions(self):
        out = self.raw(self.m.workout_insights)()
        self.assertEqual(out["error"], "request_failed")
        self.assertNotIn("total_sessions", out)

    def test_training_history_days_are_clamped(self):
        fake = mock.Mock(credentials={"token": "x"})
        fake.get_training_history.return_value = []
        with mock.patch.object(self.m, "_client", lambda: fake):
            out = self.raw(self.m.training_history)(days=10**9)
        start, end = date.fromisoformat(out["start"]), date.fromisoformat(out["end"])
        self.assertEqual((end - start).days, ic.MAX_DAYS)


if __name__ == "__main__":
    unittest.main()
