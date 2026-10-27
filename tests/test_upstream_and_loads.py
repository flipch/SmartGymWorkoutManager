"""Review round 13: vendor errors are errors (calendar, delete, save), loads are bounded and unit-explicit."""
import base64
import os
import sys
import tempfile
import unittest
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

BASIC = {"Authorization": "Basic " + base64.b64encode(f"gym:{TOKEN}".encode()).decode()}
VENDOR_TOKEN = "vendor-session-token-ABC123"
ODD = 987654.321  # a value no message may echo


class Resp:
    def __init__(self, status=200, body=None, text=None):
        self.status_code, self._body, self.text = status, body, text if text is not None else ""

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


def make_client():
    with mock.patch.dict(os.environ, {"DATA_DIR": tempfile.mkdtemp(prefix="gym-client-")}):
        client = api_client.SpeedianceClient()
    client.save_config("42", VENDOR_TOKEN, "Global", 0, "", 1, False, [], [], unit_confirmed=True)
    return client


def good_set(**kw):
    return {"reps": 10, "weight": 20.0, "rest": 60, "unit": "reps", **kw}


def good_ex(**kw):
    return {"groupId": 7, "sets": [good_set()], **kw}


class CalendarIsAnErrorNotAnEmptyMonth(unittest.TestCase):
    def month(self, resp):
        client = make_client()
        client._request = mock.Mock(side_effect=resp if isinstance(resp, Exception) else None,
                                    return_value=None if isinstance(resp, Exception) else resp)
        return client.get_calendar_month("2026-10")

    def test_http_errors_raise(self):
        for status in (400, 403, 404, 429, 500, 502, 503):
            with self.assertRaises(api_client.UpstreamError, msg=status):
                self.month(Resp(status, {"data": []}))

    def test_network_errors_raise_without_leaking_the_url_or_token(self):
        with self.assertRaises(api_client.UpstreamError) as ctx:
            self.month(ConnectionError(f"https://api2.speediance.com/x?token={VENDOR_TOKEN}"))
        self.assertIn("ConnectionError", str(ctx.exception))
        self.assertNotIn(VENDOR_TOKEN, str(ctx.exception))
        self.assertNotIn("speediance", str(ctx.exception))

    def test_malformed_bodies_raise(self):
        for resp in (Resp(200, None), Resp(200, ["x"]), Resp(200, {"code": 1, "data": []}),
                     Resp(200, {"data": {"not": "a list"}})):
            with self.assertRaises(api_client.UpstreamError):
                self.month(resp)

    def test_a_401_stays_unauthorized_so_callers_can_re_login(self):
        with self.assertRaises(Exception) as ctx:
            self.month(Resp(401, {}))
        self.assertEqual(str(ctx.exception), "Unauthorized")
        with self.assertRaises(Exception) as ctx:
            self.month(Exception("Unauthorized"))
        self.assertEqual(str(ctx.exception), "Unauthorized")

    def test_real_answers_pass_through(self):
        self.assertEqual(self.month(Resp(200, {"code": 0, "data": [{"a": 1}]})), [{"a": 1}])
        self.assertEqual(self.month(Resp(200, {"data": []})), [])  # a genuinely empty month is fine

    def test_the_ics_feed_and_calendar_api_report_the_outage(self):
        c = gym_app.app.test_client()
        failing = mock.Mock(side_effect=ConnectionError("down"))
        with mock.patch.object(gym_app.client, "credentials", {"token": VENDOR_TOKEN}), \
                mock.patch.object(gym_app.client, "_request", failing):
            ics = c.get("/calendar.ics", headers=BASIC)
            self.assertEqual((ics.status_code, ics.headers.get("Retry-After")), (503, "300"))
            self.assertNotIn(b"VCALENDAR", ics.data)
            api = c.get("/api/calendar?date=2026-10", headers=BASIC)
            self.assertEqual(api.status_code, 500)
            self.assertIn("error", api.get_json())


class DeleteIsOnlyDeletedWhenTheVendorSaysSo(unittest.TestCase):
    def delete(self, resp):
        client = make_client()
        client._request = mock.Mock(return_value=resp)
        return client.delete_workout("abc")

    def test_rejections_raise(self):
        for resp in (Resp(500, {}), Resp(404, {}), Resp(302, {}), Resp(200, {"code": 1001, "message": "no"}), Resp(200, {"code": 500}),
                     Resp(200, None, "<html>proxy login</html>"), Resp(200, None, "ok")):
            with self.assertRaises(api_client.UpstreamError):
                self.delete(resp)

    def test_401_is_unauthorized(self):
        with self.assertRaises(Exception) as ctx:
            self.delete(Resp(401, {}))
        self.assertEqual(str(ctx.exception), "Unauthorized")

    def test_successes_pass(self):
        for resp in (Resp(200, {"code": 0}), Resp(200, {}), Resp(204, None), Resp(200, None, ""), Resp(200, None, "  ")):
            self.assertIsNone(self.delete(resp))

    def test_the_route_never_says_deleted_after_a_refusal(self):
        c = gym_app.app.test_client()
        with mock.patch.dict(os.environ, {"GYM_ALLOW_WRITES": "1"}), \
                mock.patch.object(gym_app.client, "delete_workout", side_effect=api_client.UpstreamError("delete failed (HTTP 500)")):
            resp = c.post("/delete/5", headers=BASIC)
            self.assertEqual(resp.status_code, 302)
            with c.session_transaction() as s:
                flashes = s["_flashes"]
        self.assertEqual([cat for cat, _ in flashes], ["error"])
        self.assertNotIn("deleted.", flashes[0][1])


class LoadsAreBoundedBeforeAnythingIsSent(unittest.TestCase):
    def refused(self, name, exercises, fragment=None):
        with self.assertRaises(ValueError) as ctx:
            api_client.validate_workout(name, exercises)
        self.assertNotIn(str(ODD), str(ctx.exception))
        if fragment:
            self.assertIn(fragment, str(ctx.exception))

    def test_good_workouts_pass_at_the_edges(self):
        api_client.validate_workout("ok", [good_ex()])
        api_client.validate_workout("x" * 100, [good_ex(sets=[good_set(weight=100, reps=99, rest=300)] * 20)] * 30)
        api_client.validate_workout("t", [good_ex(sets=[good_set(unit="sec", reps=3600)])])
        api_client.validate_workout("p", [good_ex(preset_id=1, sets=[good_set(weight=20)])])
        api_client.validate_workout("v", [good_ex(data_stat_type=6, sets=[good_set(weight=10)])])
        api_client.validate_workout("z", [good_ex(sets=[{"reps": 1, "weight": None, "rest": None}])])  # weight/rest default
        for missing in (None, "", 0):  # a set without reps (or seconds) is a typo, not a zero-rep set
            with self.assertRaises(ValueError, msg=repr(missing)) as ctx:
                api_client.validate_workout("z", [good_ex(sets=[{"reps": missing, "weight": 5, "rest": 60}])])
            self.assertIn("reps", str(ctx.exception))

    def test_weights(self):
        for bad in (float("nan"), float("inf"), float("-inf"), -0.5, -50, 100.5, 1e7, "abc", True, "1e999", [1]):
            self.refused("w", [good_ex(sets=[good_set(weight=bad)])], "weight")
        self.refused("w", [good_ex(sets=[good_set(weight=ODD)])], "weight")

    def test_preset_and_level_ranges_are_their_own(self):
        self.refused("p", [good_ex(preset_id=1, sets=[good_set(weight=21)])], "weight")
        self.refused("v", [good_ex(data_stat_type=6, sets=[good_set(weight=11)])], "weight")

    def test_counts_and_rests(self):
        self.refused("r", [good_ex(sets=[good_set(reps=100)])], "reps")
        self.refused("r", [good_ex(sets=[good_set(reps=3601, unit="sec")])], "reps")
        self.refused("r", [good_ex(sets=[good_set(reps=-1)])], "reps")
        self.refused("r", [good_ex(sets=[good_set(rest=301)])], "rest")
        self.refused("s", [good_ex(sets=[good_set()] * 21)], "sets")
        self.refused("s", [good_ex(sets=[])], "sets")
        self.refused("e", [good_ex()] * 51, "exercises")
        self.refused("e", [], "exercises")

    def test_shapes_and_names(self):
        for name in ("", "   ", None, 5, "x" * 101):
            self.refused(name, [good_ex()], "name")
        for ex in (None, "x", {}, [None], [{"sets": "x"}], [{"groupId": 1}], [good_ex(sets=[None])]):
            self.refused("n", ex)

    def test_save_workout_refuses_before_it_calls_the_vendor(self):
        client = make_client()
        client._request = mock.Mock()
        for exercises in ([good_ex(sets=[good_set(weight=float("nan"))])], [good_ex(sets=[good_set(weight=1e9)])]):
            with self.assertRaises(ValueError):
                client.save_workout("n", exercises)
        client._request.assert_not_called()

    def test_a_single_cable_load_is_capped_at_50(self):
        client = make_client()
        client._request = mock.Mock()
        client.get_batch_details = lambda ids, **kw: [{"id": 7, "actionLibraryList": [{"id": 70}]}]
        client.is_exercise_unilateral = lambda gid, **kw: True
        with self.assertRaises(ValueError) as ctx:
            client.save_workout("n", [good_ex(sets=[good_set(weight=50.5)] * 2)])
        self.assertIn("single-cable", str(ctx.exception))
        client._request.assert_not_called()
        client._request.return_value = Resp(200, {"code": 0})
        self.assertEqual(client.save_workout("n", [good_ex(sets=[good_set(weight=50)] * 2)]), {"code": 0})

    def test_the_web_form_refuses_nan_without_calling_the_vendor(self):
        c = gym_app.app.test_client()
        with mock.patch.object(gym_app.client, "credentials", {"token": VENDOR_TOKEN}), \
                mock.patch.dict(os.environ, {"GYM_ALLOW_WRITES": "1"}), \
                mock.patch.object(gym_app.client, "_request") as request:
            resp = c.post("/create", headers=BASIC, json={"name": "n", "exercises": [good_ex(sets=[good_set(weight=float("nan"))])]})
        self.assertEqual(resp.get_json()["status"], "error")
        request.assert_not_called()


class ApplyProgram(unittest.TestCase):
    def setUp(self):
        self.client = mock.Mock()
        self.client.credentials = {"token": "x", "unit": 0, "unit_confirmed": True}
        self.client.get_library.return_value = []
        self.client.save_workout.return_value = {"code": 0, "data": {"id": 1}}
        patcher = mock.patch.object(features, "resolve_exercise",
                                    return_value={"groupId": 7, "matched_name": "Press", "muscle": "chest", "confidence": "high"})
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def program(*loads):
        return {"goal_label": "G", "days": [{"name": f"D{i}", "exercises": [
            {"name": "Press", "primary": "chest", "sets": 2, "reps": 8, "load": load, "rest_sec": 90}]}
            for i, load in enumerate(loads)]}

    def saved_weights(self):
        return [s["weight"] for s in self.client.save_workout.call_args[0][1][0]["sets"]]

    def test_pounds_are_converted_to_the_kilograms_the_api_stores(self):
        features.apply_program(self.client, self.program(110), 0, None, dry_run=False, load_unit="lb")
        self.assertEqual(self.saved_weights(), [50.0, 50.0])  # 110 lb / 2.2, like the web editor
        features.apply_program(self.client, self.program(135), 0, None, dry_run=False, load_unit="lb")
        self.assertEqual(self.saved_weights(), [61.5, 61.5])  # nearest 0.5 kg

    def test_kilograms_pass_through(self):
        features.apply_program(self.client, self.program(42.5), 0, None, dry_run=False, load_unit="kg")
        self.assertEqual(self.saved_weights(), [42.5, 42.5])

    def test_a_program_with_loads_must_name_its_unit_even_for_a_preview(self):
        for dry in (True, False):
            with self.assertRaises(ValueError):
                features.apply_program(self.client, self.program(50), 0, None, dry_run=dry)
        for bad in ("LB", "pounds", "", 1, True):
            with self.assertRaises(ValueError):
                features.apply_program(self.client, self.program(50), 0, None, dry_run=True, load_unit=bad)
        self.client.save_workout.assert_not_called()

    def test_a_program_without_loads_needs_no_unit(self):
        out = features.apply_program(self.client, self.program(0, None), None, None, dry_run=False)
        self.assertEqual(out["created"], 2)

    def test_non_numeric_loads_are_refused(self):
        with self.assertRaises(ValueError):
            features.apply_program(self.client, self.program("heavy"), 0, None, dry_run=True, load_unit="kg")

    def test_one_bad_day_stops_everything_before_any_write(self):
        with self.assertRaises(ValueError):
            features.apply_program(self.client, self.program(40, 400), None, None, dry_run=False, load_unit="kg")
        self.client.save_workout.assert_not_called()

    def test_a_wrong_unit_that_would_exceed_the_cap_is_caught(self):
        # 225 lb is a normal load; saved as kg it would be 225 kg, so the unit matters: as lb it is 102.5 kg -> still refused,
        # as the web editor refuses over 100 kg too
        with self.assertRaises(ValueError):
            features.apply_program(self.client, self.program(225), 0, None, dry_run=False, load_unit="kg")
        with self.assertRaises(ValueError):
            features.apply_program(self.client, self.program(225), 0, None, dry_run=False, load_unit="lb")
        features.apply_program(self.client, self.program(200), 0, None, dry_run=False, load_unit="lb")  # 91 kg

    def test_a_vendor_rejection_is_a_failure_not_a_created_template(self):
        for answer in ({"code": 1001, "message": "Parameter Error"}, {"data": {"id": 1}}, {}, None, "ok", []):
            self.client.save_workout.return_value = answer
            out = features.apply_program(self.client, self.program(0), 0, None, dry_run=False)
            self.assertEqual((out["created"], out["failed"]), (0, 1), answer)
            self.assertEqual(out["results"][0]["error"], "UpstreamRejected")
            self.assertFalse(out["results"][0]["created"])

    def test_the_vendor_code_is_reported(self):
        self.client.save_workout.return_value = {"code": 1001}
        out = features.apply_program(self.client, self.program(0), 0, None, dry_run=False)
        self.assertEqual(out["results"][0]["upstream_code"], 1001)

    def test_the_endpoint_maps_unit_errors_to_400_and_passes_the_unit_through(self):
        c = gym_app.app.test_client()
        with mock.patch.object(features, "_client", self.client), mock.patch.dict(os.environ, {"GYM_ALLOW_WRITES": "1"}):
            self.client.credentials = {"token": "x", "unit": 0, "unit_confirmed": True}
            resp = c.post("/api/coach/apply", headers=BASIC, json={"program": self.program(50), "day_index": 0})
            self.assertEqual((resp.status_code, resp.get_json()["error"]), (400, "bad_request"))
            ok = c.post("/api/coach/apply", headers=BASIC,
                        json={"program": self.program(110), "day_index": 0, "load_unit": "lb", "dry_run": False})
            self.assertEqual(ok.status_code, 200)
        self.assertEqual(self.saved_weights(), [50.0, 50.0])

    def test_the_mcp_tool_takes_the_unit_and_converts_like_the_endpoint(self):
        import mcp_server
        tool = getattr(mcp_server.create_workout_from_program, "fn", mcp_server.create_workout_from_program)
        with mock.patch.dict(os.environ, {"GYM_ALLOW_WRITES": "1"}), mock.patch.object(mcp_server, "_client", return_value=self.client), \
                mock.patch.object(mcp_server, "_require_auth"):
            refused = tool(self.program(110), day_index=0, dry_run=False)
            self.assertIn("error", refused)  # loads without a unit
            self.client.save_workout.assert_not_called()
            ok = tool(self.program(110), day_index=0, dry_run=False, load_unit="lb")
        self.assertEqual(ok["created"], 1)
        self.assertEqual(self.saved_weights(), [50.0, 50.0])


if __name__ == "__main__":
    unittest.main()
