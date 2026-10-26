"""Review round 16: the unit gate covers every save path; /settings never resets a confirmed unit; an HTTP-200 vendor error
is a hole in the library; the MATCHED exercise decides left/right; opaque template ids; calorie and unknown units are
refused; an ACWR-only reading reaches the program."""
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
sys.path.insert(0, os.path.dirname(__file__))

import api_client  # noqa: E402
import app as gym_app  # noqa: E402
import features  # noqa: E402
import mcp_server  # noqa: E402

BASIC = {"Authorization": "Basic " + base64.b64encode(f"gym:{TOKEN}".encode()).decode()}


class Resp:
    def __init__(self, status=200, body=None, text=""):
        self.status_code, self._body, self.text = status, body, text
        self.content = b"x"
        self.request = mock.Mock(headers={})

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


def client_with(unit, confirmed, calls=None):
    with mock.patch.dict(os.environ, {"DATA_DIR": tempfile.mkdtemp(prefix="gym-client-")}):
        client = api_client.SpeedianceClient()
    client.save_config("42", "vendor-token", "Global", unit, "", 1, False, [], [], unit_confirmed=confirmed)
    client.get_batch_details = lambda ids, **kw: [{"id": i, "actionLibraryList": [{"id": i + 1000}]} for i in ids]
    client.is_exercise_unilateral = lambda gid, **kw: False

    def request(method, url, **kw):
        if calls is not None:
            calls.append((method, url, kw))
        return Resp(200, {"code": 0, "data": {"id": 1}})

    client._request = request
    return client


def ex(weight=20.0, **kw):
    return {"groupId": 7, "sets": [{"reps": 10, "weight": weight, "rest": 60, "unit": "reps"}], **kw}


class TheUnitGateCoversEverySavePath(unittest.TestCase):
    def test_an_unconfirmed_metric_default_cannot_save_a_cable_load(self):
        calls = []
        client = client_with(0, False, calls)
        with self.assertRaises(ValueError) as ctx:
            client.save_workout("n", [ex(40)])
        self.assertIn("/settings", str(ctx.exception))
        self.assertEqual(calls, [])

    def test_confirmed_or_imperial_accounts_save(self):
        for unit, confirmed in ((0, True), (1, False), (1, True)):
            self.assertEqual(client_with(unit, confirmed).save_workout("n", [ex(40)])["code"], 0, (unit, confirmed))

    def test_numbers_that_are_not_a_unit_bearing_load_are_not_gated(self):
        client = client_with(0, False)
        client.save_workout("zero", [ex(0)])
        client.save_workout("rm", [ex(10, preset_id=1)])
        client.save_workout("vita", [ex(5, data_stat_type=6)])

    def test_the_web_route_and_the_mcp_tool_hit_the_same_gate(self):
        calls = []
        client = client_with(0, False, calls)
        with mock.patch.object(gym_app, "client", client), mock.patch.dict(os.environ, {"GYM_ALLOW_WRITES": "1"}):
            resp = gym_app.app.test_client().post("/create", headers=BASIC, json={"name": "n", "exercises": [ex(40)]})
        self.assertEqual(resp.get_json()["status"], "error")
        self.assertIn("/settings", resp.get_json()["message"])
        tool = getattr(mcp_server.save_workout, "fn", mcp_server.save_workout)
        with mock.patch.object(mcp_server, "_client", return_value=client), mock.patch.object(mcp_server, "_require_auth"), \
                mock.patch.dict(os.environ, {"GYM_ALLOW_WRITES": "1"}):
            out = tool("n", [ex(40)])
        self.assertIn("error", out)
        self.assertEqual(calls, [])


class SettingsNeverResetsAConfirmedUnit(unittest.TestCase):
    def test_a_form_without_the_unit_field_keeps_the_unit(self):
        client = client_with(1, True)
        with mock.patch.object(gym_app, "client", client):
            resp = gym_app.app.test_client().post("/settings", headers=BASIC, data={"custom_instruction": "be nice"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual((client.credentials["unit"], client.credentials["unit_confirmed"]), (1, True))
        self.assertEqual(client.credentials["custom_instruction"], "be nice")

    def test_choosing_a_unit_sets_it_and_confirms_it(self):
        client = client_with(0, False)
        with mock.patch.object(gym_app, "client", client):
            gym_app.app.test_client().post("/settings", headers=BASIC, data={"unit": "1"})
        self.assertEqual((client.credentials["unit"], client.credentials["unit_confirmed"]), (1, True))


class AnHttp200VendorErrorIsAHoleInTheLibrary(unittest.TestCase):
    def library_client(self, second_body):
        with mock.patch.dict(os.environ, {"DATA_DIR": tempfile.mkdtemp(prefix="gym-lib-")}):
            client = api_client.SpeedianceClient()
        client.save_config("1", "tok", "Global", 0, "", 1, False, [], [])
        client.library_cache = None
        client.get_categories = lambda strict=False: [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]
        ok = Resp(200, {"code": 0, "data": [{"actionLibraryGroupList": [{"id": 5, "name": "Press"}]}]})
        client._request = lambda method, url, **kw: ok if "tabId=1" in url else Resp(200, second_body)
        client.get_batch_details = lambda ids, strict=False: [{"id": i, "name": "Press"} for i in ids]
        return client

    def test_code_500_with_empty_data_is_incomplete_and_not_cached(self):
        client = self.library_client({"code": 500, "data": []})
        with self.assertRaises(api_client.UpstreamError):
            client.get_library(strict=True)
        self.assertFalse(client.library_cache)

    def test_a_clean_empty_category_is_not_a_hole(self):
        client = self.library_client({"code": 0, "data": []})
        self.assertEqual([e["id"] for e in client.get_library(strict=True)], [5])


class TheMatchedExerciseDecidesLeftRight(unittest.TestCase):
    LIB = [{"id": 1, "title": "Chest Press", "mainMuscleGroupName": "chest", "isLeftRight": 0},
           {"id": 2, "title": "Single Arm Chest Press Unique", "mainMuscleGroupName": "chest", "isLeftRight": 1}]

    @staticmethod
    def day(**ex):
        base = {"name": "Chest Press", "primary": "chest", "sets": 3, "reps": 8, "load": 0, "rest_sec": 60}
        base.update(ex)
        return {"name": "D", "exercises": [base]}

    def test_resolve_exercise_reports_the_match_flag(self):
        self.assertFalse(features.resolve_exercise(self.LIB, "Chest Press", "chest")["unilateral"])
        self.assertTrue(features.resolve_exercise(self.LIB, "Single Arm Chest Press Unique", "chest", True)["unilateral"])

    def test_a_bilateral_coach_move_matched_to_a_one_sided_exercise_is_paired(self):
        with mock.patch.object(features, "resolve_exercise",
                               return_value={"groupId": 2, "matched_name": "Single Arm", "confidence": "high", "unilateral": True}):
            sets = features.program_day_to_payload([], self.day(unilateral=False), "kg", False)[0][0]["sets"]
        self.assertEqual(len(sets), 6)  # three pairs, not left/right/left

    def test_a_one_sided_coach_move_matched_to_a_bilateral_exercise_is_not_doubled(self):
        with mock.patch.object(features, "resolve_exercise",
                               return_value={"groupId": 1, "matched_name": "Chest Press", "confidence": "high", "unilateral": False}):
            sets = features.program_day_to_payload([], self.day(unilateral=True), "kg", False)[0][0]["sets"]
        self.assertEqual(len(sets), 3)


class TemplateIdsCanBeOpaque(unittest.TestCase):
    def test_a_hex_id_deletes_and_updates(self):
        calls = []
        client = client_with(1, True, calls)
        client.delete_workout("6600aabbccddeeff")
        self.assertTrue(calls[-1][1].endswith("ids=6600aabbccddeeff"))
        client.save_workout("n", [ex(40)], "6600aabbccddeeff")
        self.assertEqual(client.prepare_workout("n", [ex(40)], "6600aabbccddeeff")["id"], "6600aabbccddeeff")
        self.assertEqual(client.prepare_workout("n", [ex(40)], "77")["id"], 77)
        self.assertNotIn("id", client.prepare_workout("n", [ex(40)]))

    def test_a_hostile_id_is_refused(self):
        client = client_with(1, True)
        for bad in ("1/../x", "1,2", "a b", "x" * 300):
            with self.assertRaises(ValueError, msg=bad):
                client.prepare_workout("n", [ex(40)], bad)

    def test_the_delete_route_takes_opaque_ids_and_404s_the_rest(self):
        client = mock.Mock()
        client.credentials = {"token": "x"}
        with mock.patch.object(gym_app, "client", client), mock.patch.dict(os.environ, {"GYM_ALLOW_WRITES": "1"}):
            c = gym_app.app.test_client()
            self.assertEqual(c.post("/delete/6600aabbccddeeff", headers=BASIC).status_code, 302)
            client.delete_workout.assert_called_once_with("6600aabbccddeeff")
            self.assertEqual(c.post("/delete/a.b", headers=BASIC).status_code, 404)
            self.assertEqual(client.delete_workout.call_count, 1)


class UnitsTheSerializerDoesNotKnowAreRefused(unittest.TestCase):
    def check(self, unit):
        api_client.validate_workout("n", [{"groupId": 7, "sets": [{"reps": 10, "weight": 10, "rest": 60, "unit": unit}]}])

    def test_calorie_targets_are_refused_by_name(self):
        for unit in ("kcal", "KCAL"):
            with self.assertRaises(ValueError) as ctx:
                self.check(unit)
            self.assertIn("calorie", str(ctx.exception))

    def test_unknown_units_are_refused_and_known_ones_pass(self):
        for unit in ("minutes", "cal", 5, True):
            with self.assertRaises(ValueError, msg=repr(unit)):
                self.check(unit)
        for unit in ("reps", "sec", "SEC", None, ""):
            self.check(unit)


class AnAcwrOnlyReadingReachesTheProgram(unittest.TestCase):
    def test_the_signal_list_includes_every_field_the_validator_accepts(self):
        import state_io
        self.assertEqual(set(state_io.validate_readiness({"whoop_recovery": 50, "hrv_vs_baseline": "within", "rhr_delta_bpm": 1,
                                                          "sleep_hours": 7, "subjective": 3, "acwr": 1.0})),
                         set(state_io.READINESS_SIGNALS))

    def test_the_web_route_and_the_mcp_tool_apply_a_stored_acwr(self):
        with mock.patch.object(features, "_fresh_readiness", return_value={"acwr": 2.0}), \
                mock.patch.object(features, "_client", mock.Mock(data_dir=tempfile.mkdtemp())):
            resp = gym_app.app.test_client().post("/api/coach/program", headers=BASIC, json={"goal": "muscle"})
        self.assertIsNotNone(resp.get_json()["autoregulation"])
        client = mock.Mock(data_dir=tempfile.mkdtemp())
        tool = getattr(mcp_server.generate_program, "fn", mcp_server.generate_program)
        with mock.patch.object(mcp_server, "_client", return_value=client), mock.patch.object(mcp_server, "_require_auth"), \
                mock.patch.object(mcp_server, "_fresh_readiness", return_value={"acwr": 2.0}):
            out = tool(goal="muscle", days_per_week=3, experience="intermediate")
        self.assertIsNotNone(out["autoregulation"])


class TheCreateRouteSurvivesAnyBody(unittest.TestCase):
    def test_a_non_object_body_is_an_error_message_not_a_500(self):
        client = client_with(1, True)
        with mock.patch.object(gym_app, "client", client), mock.patch.dict(os.environ, {"GYM_ALLOW_WRITES": "1"}):
            for body in ([1], "x", 5):
                resp = gym_app.app.test_client().post("/create", headers=BASIC, json=body)
                self.assertLess(resp.status_code, 500, body)


class TheUnitFlagOnlyComesFromAnActualChoice(unittest.TestCase):
    def post_settings(self, client, path, **form):
        with mock.patch.object(gym_app, "client", client), mock.patch.dict(os.environ, {"GYM_ALLOW_WRITES": "1"}):
            return gym_app.app.test_client().post(path, headers=BASIC, data=form)

    def test_saving_the_manual_config_never_confirms_a_unit(self):
        client = client_with(0, False)
        self.post_settings(client, "/settings", region="EU", user_id="42", custom_instruction="x")
        self.assertFalse(client.credentials["unit_confirmed"])
        text = open(os.path.join(os.path.dirname(__file__), "..", "templates", "settings.html")).read()
        self.assertNotIn('type="hidden" name="unit"', text)  # the hidden "preserve" field is what forged the choice

    def test_invalid_units_and_device_types_change_nothing(self):
        client = client_with(1, True)
        for form in ({"unit": "7"}, {"unit": "abc"}, {"unit": "-1"}, {"device_type": "x"}, {"device_type": "9"}):
            resp = self.post_settings(client, "/settings", **form)
            self.assertLess(resp.status_code, 500, form)
            self.assertEqual((client.credentials["unit"], client.credentials["device_type"]), (1, 1), form)

    def test_the_unit_endpoint_only_takes_0_or_1(self):
        client = mock.Mock()
        client.credentials = {"token": "x"}
        client.update_unit.return_value = (True, "ok")
        for bad in (None, "7", "abc", "", "1.0"):
            resp = self.post_settings(client, "/settings/unit", **({} if bad is None else {"unit": bad}))
            self.assertLess(resp.status_code, 500, bad)
        client.update_unit.assert_not_called()
        self.post_settings(client, "/settings/unit", unit="1")
        client.update_unit.assert_called_once_with("1")

    def test_a_different_account_does_not_inherit_the_unit(self):
        client = client_with(1, True)
        client.save_config("42", "tok2", "Global", 1, "", 1, False, [], [])  # same account re-login: keeps it
        self.assertEqual((client.credentials["unit"], client.credentials["unit_confirmed"]), (1, True))
        client.save_config("999", "tok3", "Global", 1, "", 1, False, [], [])
        self.assertEqual((client.credentials["unit"], client.credentials["unit_confirmed"]), (0, False))
        client.save_config("1000", "tok4", "Global", 1, "", 1, False, [], [], unit_confirmed=True)  # an explicit choice wins
        self.assertEqual((client.credentials["unit"], client.credentials["unit_confirmed"]), (1, True))

    def test_the_custom_instruction_endpoint_survives_any_body(self):
        client = client_with(1, True)
        for body in ([1], None, "x", {"instruction": ["a"]}, {"instruction": "x" * 4001}):
            with mock.patch.object(gym_app, "client", client):
                resp = gym_app.app.test_client().post("/settings/custom_instruction", headers=BASIC, json=body)
            self.assertLess(resp.status_code, 500, body)
        with mock.patch.object(gym_app, "client", client):
            resp = gym_app.app.test_client().post("/settings/custom_instruction", headers=BASIC, json={"instruction": "hi"})
        self.assertEqual((resp.status_code, client.credentials["custom_instruction"]), (200, "hi"))
        with mock.patch.object(gym_app, "client", client):
            post = gym_app.app.test_client().post
            self.assertEqual(post("/settings/custom_instruction", headers=BASIC, json={"instruction": "x" * 4001}).status_code, 400)
            self.assertEqual(post("/settings/custom_instruction", headers=BASIC, json={"instruction": "x" * 4000}).status_code, 200)


class AnOutageInTheCategoryListIsNotAnEmptyGym(unittest.TestCase):
    def library_client(self, responder):
        with mock.patch.dict(os.environ, {"DATA_DIR": tempfile.mkdtemp(prefix="gym-lib-")}):
            client = api_client.SpeedianceClient()
        client.save_config("1", "tok", "Global", 0, "", 1, False, [], [])
        client.library_cache = None
        client._request = responder
        return client

    def test_a_502_page_raises_under_strict_and_caches_nothing(self):
        client = self.library_client(lambda m, u, **kw: Resp(502, None, "<html>bad gateway</html>"))
        with self.assertRaises(api_client.UpstreamError):
            client.get_library(strict=True)
        self.assertFalse(client.library_cache)
        self.assertFalse(os.path.exists(client.library_cache_file))
        with self.assertRaises(api_client.UpstreamError):  # the lenient path no longer turns an outage into an empty gym
            client.get_library()
        self.assertFalse(os.path.exists(client.library_cache_file))

    def test_zero_categories_is_an_outage_not_an_empty_library(self):
        client = self.library_client(lambda m, u, **kw: Resp(200, {"code": 0, "data": []}))
        with self.assertRaises(api_client.UpstreamError):
            client.get_library(strict=True)
        self.assertFalse(os.path.exists(client.library_cache_file))

    def test_an_expired_session_is_still_unauthorized(self):
        client = self.library_client(lambda m, u, **kw: Resp(401, {}))
        with self.assertRaises(Exception) as ctx:
            client.get_library(strict=True)
        self.assertEqual(str(ctx.exception), "Unauthorized")

    def test_get_categories_strict_raises_and_lenient_does_not(self):
        client = self.library_client(lambda m, u, **kw: Resp(503, {}))
        self.assertEqual(client.get_categories(), [])
        with self.assertRaises(api_client.UpstreamError):
            client.get_categories(strict=True)


class AnUnreadableAvoidListStopsTheWrites(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(features, "resolve_exercise", return_value=dict(
            {"groupId": 7, "matched_name": "Cable Chest Press", "muscle": "chest", "confidence": "high"}))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.dir = tempfile.mkdtemp(prefix="gym-fb-")
        self.client = mock.Mock(data_dir=self.dir, credentials={"token": "x", "unit": 1})
        self.client.get_library.return_value = []
        self.client.save_workout.return_value = {"code": 0, "data": {"id": 1}}
        self.program = {"goal_label": "G", "days": [{"name": "D", "exercises": [
            {"name": "Press", "primary": "chest", "sets": 2, "reps": 8, "load": 0, "rest_sec": 60}]}]}

    def corrupt(self):
        with open(features._feedback_path(self.dir), "w") as f:
            f.write("{corrupt")

    def test_a_corrupt_file_refuses_a_save_and_flags_a_preview(self):
        self.corrupt()
        with self.assertRaises(ValueError) as ctx:
            features.apply_program(self.client, self.program, 0, None, dry_run=False)
        self.assertIn("coach_feedback.json", str(ctx.exception))
        self.client.save_workout.assert_not_called()
        preview = features.apply_program(self.client, self.program, 0, None, dry_run=True)
        self.assertEqual(preview["feedback_error"], "JSONDecodeError")

    def test_a_healthy_or_absent_file_is_silent(self):
        out = features.apply_program(self.client, self.program, 0, None, dry_run=False)
        self.assertNotIn("feedback_error", out)

    def test_a_quarantined_file_is_reported_but_does_not_block(self):
        features.log_feedback({"pain": True, "exercise": "Row"}, self.dir)
        self.corrupt()
        features.log_feedback({"rpe": 7}, self.dir)  # quarantines the corrupt file and starts again
        self.assertEqual(features._feedback_read_error(self.dir), features.QUARANTINED)
        out = features.apply_program(self.client, self.program, 0, None, dry_run=False)
        self.assertEqual((out["created"], out["feedback_error"]), (1, features.QUARANTINED))
        prog = features._feedback_profile(self.dir)
        self.assertEqual(prog["unreadable"], features.QUARANTINED)


class HostileInputsAreFourHundredsNotFiveHundreds(unittest.TestCase):
    def post(self, path, body):
        fake = mock.Mock(data_dir=tempfile.mkdtemp(prefix="gym-body-"), credentials={"token": "x"})
        with mock.patch.object(features, "_client", fake), mock.patch.object(features, "_authed", return_value=False):
            return gym_app.app.test_client().post(path, headers=BASIC, json=body)

    def test_the_program_route(self):
        for body in ({"days_per_week": "x"}, {"one_rm": [1, 2]}, {"one_rm": {"bench": "heavy"}},
                     {"feedback": [1]}, {"available_accessories": 5}):
            self.assertLess(self.post("/api/coach/program", body).status_code, 500, body)

    def test_the_critique_route(self):
        for body in ({"exercises": [{"name": "A", "sets": "x", "reps": 5}]}, {"exercises": [1, 2]}, {"exercises": "nope"}):
            self.assertLess(self.post("/api/coach/critique", body).status_code, 500, body)

    def test_mcp_autoregulate_refuses_out_of_range_readings(self):
        tool = getattr(mcp_server.autoregulate, "fn", mcp_server.autoregulate)
        self.assertIn("error", tool(whoop_recovery=1000.0, subjective=50))
        self.assertIn("error", tool(whoop_recovery=float("nan")))
        self.assertIn("band", tool(whoop_recovery=70.0))

    def test_mcp_schedule_validates_before_the_vendor_call(self):
        client = mock.Mock()
        tool = getattr(mcp_server.schedule_workout, "fn", mcp_server.schedule_workout)
        with mock.patch.object(mcp_server, "_client", return_value=client), mock.patch.object(mcp_server, "_require_auth"), \
                mock.patch.dict(os.environ, {"GYM_ALLOW_WRITES": "1"}):
            for day, code in (("tomorrow", "abc"), ("2026-W43-1", "abc"), ("2026-10-27", "a/../b"), ("2026-10-27", "a,b")):
                self.assertIn("error", tool(day, code), (day, code))
            client.schedule_workout.assert_not_called()
            client.schedule_workout.return_value = True
            self.assertTrue(tool("2026-10-27", "abc123")["ok"])

    def test_iso_week_and_compact_dates_are_not_iso_dates(self):
        from insights_core import valid_iso_date
        for bad in ("2026-W43-1", "20261027", "2026-10-27T00:00", "2026-1-5", " 2026-10-27", "2026-10-27\n"):
            self.assertFalse(valid_iso_date(bad), bad)
        self.assertTrue(valid_iso_date("2026-10-27"))


class CalendarHeuristicsIgnoreBookkeepingKeys(unittest.TestCase):
    def walk(self, node):
        out = []
        features._walk_events(node, out)
        return [(str(d), n) for d, n in out]

    def test_a_plans_create_date_does_not_beat_its_days_date(self):
        node = {"date": "2026-10-27", "trainingPlanList": [{"title": "Push", "createDate": "2026-01-05", "updateDate": "2026-02-01"}]}
        self.assertEqual(self.walk(node), [("2026-10-27", "Push")])

    def test_a_week_label_on_the_day_does_not_hide_its_plans(self):
        node = {"date": "2026-10-27", "weekName": "Tuesday", "trainingPlanList": [{"title": "Push"}]}
        self.assertEqual(self.walk(node), [("2026-10-27", "Push")])


if __name__ == "__main__":
    unittest.main()
