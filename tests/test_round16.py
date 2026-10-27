"""Review round 16: the unit gate covers every save path; /settings never resets a confirmed unit; an HTTP-200 vendor error
is a hole in the library; the MATCHED exercise decides left/right; opaque template ids; calorie and unknown units are
refused; an ACWR-only reading reaches the program."""
import base64
import json
import os
import shutil
import subprocess
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
        for unit, confirmed in ((0, True), (1, True)):
            self.assertEqual(client_with(unit, confirmed).save_workout("n", [ex(40)])["code"], 0, (unit, confirmed))
        with self.assertRaises(ValueError):
            client_with(1, False).save_workout("n", [ex(40)])  # imperial by default is still unchosen

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


class Round17ServerSide(unittest.TestCase):
    def test_a_denormalised_preset_id_is_still_a_custom_load_for_the_gate(self):
        client = client_with(0, False)
        for preset in ("-01", " -1", -1, "-1", None):
            with self.assertRaises(ValueError, msg=repr(preset)) as ctx:
                client.prepare_workout("n", [ex(20, **({} if preset is None else {"preset_id": preset}))])
            self.assertIn("/settings", str(ctx.exception))
        client.prepare_workout("n", [ex(10, preset_id="1")])  # a real RM preset is not a kg/lb load
        client.prepare_workout("n", [ex(10, preset_id=1)])

    def test_the_gate_and_the_serializer_agree_on_every_preset_spelling(self):
        client = client_with(1, True)
        for preset in ("-01", "-1", -1):
            self.assertEqual(client.prepare_workout("n", [ex(20, preset_id=preset)])["actionLibraryList"][0]["templatePresetId"], -1)

    def test_a_batch_that_drops_an_exercise_is_a_hole(self):
        with mock.patch.dict(os.environ, {"DATA_DIR": tempfile.mkdtemp(prefix="gym-lib-")}):
            client = api_client.SpeedianceClient()
        client.save_config("1", "tok", "Global", 0, "", 1, False, [], [])
        client.library_cache = None
        client.get_categories = lambda strict=False: [{"id": 1, "name": "A"}]
        client._request = lambda m, u, **kw: Resp(200, {"code": 0, "data": [{"actionLibraryGroupList": [
            {"id": 7, "name": "Press"}, {"id": 8, "name": "Row"}]}]})
        client.get_batch_details = lambda ids, strict=False: [{"id": 7, "name": "Press"}]  # 8 silently missing
        with self.assertRaises(api_client.UpstreamError):
            client.get_library(strict=True)
        self.assertFalse(os.path.exists(client.library_cache_file))
        client.get_batch_details = lambda ids, strict=False: [{"id": i, "name": "x"} for i in ids]
        self.assertEqual(sorted(e["id"] for e in client.get_library(strict=True)), [7, 8])

    def test_a_calendar_node_with_only_a_creation_time_is_not_an_event(self):
        out = []
        features._walk_events({"title": "Push", "createTime": "2026-01-05"}, out)
        self.assertEqual(out, [])
        features._walk_events({"title": "Push", "startTime": "2026-10-27 18:00:00"}, out)
        self.assertEqual([(str(d), n) for d, n in out], [("2026-10-27", "Push")])


class ReadinessPartialUpdatesMerge(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="gym-ready-")

    def test_a_later_partial_post_does_not_erase_an_earlier_red_signal(self):
        import coach
        features.save_readiness({"whoop_recovery": 20}, self.dir)
        merged = features.save_readiness({"sleep_hours": 8}, self.dir)
        self.assertEqual((merged["whoop_recovery"], merged["sleep_hours"]), (20, 8))
        self.assertEqual(coach.autoregulate(features._fresh_readiness(self.dir))["band"], "low")

    def test_a_newer_value_for_the_same_field_wins(self):
        features.save_readiness({"whoop_recovery": 20}, self.dir)
        self.assertEqual(features.save_readiness({"whoop_recovery": 90}, self.dir)["whoop_recovery"], 90)

    def test_each_field_ages_out_on_its_own(self):
        from datetime import datetime, timedelta, timezone
        old = (datetime.now(timezone.utc) - timedelta(hours=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
        new = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        import json as _json
        with open(features._readiness_path(self.dir), "w") as f:
            _json.dump({"whoop_recovery": 20, "sleep_hours": 8, "updated": new, "field_updated": {"whoop_recovery": old, "sleep_hours": new}}, f)
        fresh = features._fresh_readiness(self.dir)
        self.assertNotIn("whoop_recovery", fresh)  # three days old: not today's recovery
        self.assertEqual(fresh["sleep_hours"], 8)
        saved = features.save_readiness({"subjective": 3}, self.dir)
        self.assertNotIn("whoop_recovery", saved)
        self.assertEqual((saved["sleep_hours"], saved["subjective"]), (8, 3))

    def test_a_carried_field_keeps_its_own_age(self):
        from datetime import datetime, timedelta, timezone
        import json as _json
        old = (datetime.now(timezone.utc) - timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(features._readiness_path(self.dir), "w") as f:
            _json.dump({"whoop_recovery": 20, "updated": old, "field_updated": {"whoop_recovery": old}}, f)
        saved = features.save_readiness({"sleep_hours": 8}, self.dir)
        self.assertEqual(saved["field_updated"]["whoop_recovery"], old)  # not refreshed by an unrelated post
        self.assertNotEqual(saved["field_updated"]["sleep_hours"], old)

    def test_a_reading_from_before_per_field_stamps_uses_its_single_stamp(self):
        import json as _json
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(features._readiness_path(self.dir), "w") as f:
            _json.dump({"whoop_recovery": 30, "updated": now}, f)
        self.assertEqual(features._fresh_readiness(self.dir)["whoop_recovery"], 30)
        self.assertEqual(features.save_readiness({"sleep_hours": 7}, self.dir)["whoop_recovery"], 30)


class TheUnitSurvivesALogoutOfTheSameAccountOnly(unittest.TestCase):
    def test_logout_then_the_same_account_keeps_the_choice(self):
        client = client_with(1, True)
        client._request = lambda *a, **k: Resp(200, {})
        client.logout()
        self.assertEqual(client.credentials["token"], "")
        client.save_config("42", "new-token", "Global", 1, "", 1, False, [], [])
        self.assertEqual((client.credentials["unit"], client.credentials["unit_confirmed"]), (1, True))

    def test_logout_then_another_account_does_not_inherit_it(self):
        client = client_with(1, True)
        client._request = lambda *a, **k: Resp(200, {})
        client.logout()
        client.save_config("999", "tok", "Global", 1, "", 1, False, [], [])
        self.assertEqual((client.credentials["unit"], client.credentials["unit_confirmed"]), (0, False))

    def test_the_label_never_calls_an_unchosen_unit_kilograms(self):
        self.assertEqual(features._unit_label(client_with(1, False)), "lbs")
        self.assertEqual(features._unit_label(client_with(0, True)), "kg")
        self.assertEqual(features._unit_label(client_with(0, False)), "unconfirmed")
        self.assertEqual(features._unit_label(None), "unconfirmed")

    def test_whoami_says_unknown_for_an_unchosen_unit(self):
        tool = getattr(mcp_server.whoami, "fn", mcp_server.whoami)
        for client, expect in ((client_with(0, False), "unknown"), (client_with(0, True), "metric"), (client_with(1, True), "imperial"),
                                (client_with(1, False), "unknown")):
            with mock.patch.object(mcp_server, "_client", return_value=client):
                self.assertEqual(tool()["unit"], expect)

    def test_the_settings_page_does_not_preselect_metric_for_an_unchosen_unit(self):
        client = client_with(0, False)
        with mock.patch.object(gym_app, "client", client):
            html = gym_app.app.test_client().get("/settings", headers=BASIC).get_data(as_text=True)
        self.assertIn("Not set: choose yours", html)
        client = client_with(0, True)
        with mock.patch.object(gym_app, "client", client):
            html = gym_app.app.test_client().get("/settings", headers=BASIC).get_data(as_text=True)
        self.assertNotIn("Not set: choose yours", html)


class NonFiniteNumbersAreFourHundreds(unittest.TestCase):
    def post(self, path, body):
        fake = mock.Mock(data_dir=tempfile.mkdtemp(prefix="gym-body-"), credentials={"token": "x"})
        with mock.patch.object(features, "_client", fake), mock.patch.object(features, "_authed", return_value=False):
            return gym_app.app.test_client().post(path, headers=BASIC, data=body, content_type="application/json")

    def test_infinity_and_overflowing_numbers(self):
        huge = "9" * 400  # an integer no float can hold
        for path, body in (("/api/coach/program", '{"days_per_week": Infinity}'), ("/api/coach/program", '{"one_rm": {"chest": 1e999}}'),
                           ("/api/coach/program", '{"days_per_week": NaN}'),
                           ("/api/coach/program", '{"one_rm": {"chest": %s}}' % huge),
                           ("/api/coach/critique", '{"exercises": [{"name": "A", "sets": Infinity, "reps": 5}]}'),
                           ("/api/coach/critique", '{"exercises": [{"name": "A", "sets": 3, "reps": Infinity}]}')):
            resp = self.post(path, body)
            self.assertEqual(resp.status_code, 400, (path, body[:60]))

    def test_huge_counts_are_clamped_not_crashed(self):
        huge = "9" * 400
        for path, body in (("/api/coach/program", '{"days_per_week": %s}' % huge),
                           ("/api/coach/critique", '{"exercises": [{"name": "A", "sets": %s, "reps": 5}]}' % huge)):
            self.assertLess(self.post(path, body).status_code, 500, path)

    def test_ordinary_numbers_still_work(self):
        self.assertEqual(self.post("/api/coach/program", '{"days_per_week": 3, "goal": "muscle"}').status_code, 200)


class OneSidedExercisesNeedPairs(unittest.TestCase):
    def client(self):
        client = client_with(1, True)
        client.is_exercise_unilateral = lambda gid, **kw: True
        return client

    def test_an_odd_number_of_sets_is_refused_and_pairs_pass(self):
        client = self.client()
        three = ex(40)
        three["sets"] = three["sets"] * 3
        with self.assertRaises(ValueError) as ctx:
            client.prepare_workout("n", [three])
        self.assertIn("left/right pairs", str(ctx.exception))
        four = ex(40)
        four["sets"] = four["sets"] * 4
        self.assertEqual(client.prepare_workout("n", [four])["actionLibraryList"][0]["leftRight"], "1,2,1,2")

    def test_bilateral_exercises_may_have_any_count(self):
        client = client_with(1, True)
        three = ex(40)
        three["sets"] = three["sets"] * 3
        self.assertEqual(client.prepare_workout("n", [three])["actionLibraryList"][0]["leftRight"], "0,0,0")


class OnlyConfidentLibraryMatchesAreSaved(unittest.TestCase):
    def test_a_low_confidence_match_is_reported_not_saved(self):
        day = {"name": "D", "exercises": [{"name": "Cable chest press", "primary": "chest", "sets": 3, "reps": 8, "load": 0, "rest_sec": 60}]}
        with mock.patch.object(features, "resolve_exercise",
                               return_value={"groupId": 9, "matched_name": "Overhead Press", "confidence": "low"}):
            exercises, resolved, unresolved = features.program_day_to_payload([], day, "kg", False)
        self.assertEqual((exercises, resolved), ([], []))
        self.assertIn("low-confidence", unresolved[0]["reason"])
        self.assertIn("Overhead Press", unresolved[0]["reason"])
        for conf in ("med", "high"):
            with mock.patch.object(features, "resolve_exercise", return_value={"groupId": 9, "matched_name": "x", "confidence": conf}):
                self.assertEqual(len(features.program_day_to_payload([], day, "kg", False)[0]), 1)


class VendorTimestampsInSecondsOrMilliseconds(unittest.TestCase):
    def test_both_units_give_the_same_local_time(self):
        self.assertEqual(features._epoch_to_local(1790000000), features._epoch_to_local(1790000000000))

    def test_a_session_summary_survives_millisecond_timestamps(self):
        for ts in (1790000000, 1790000000000):
            out = features._summarize_session({"startTimestamp": ts, "type": 5, "trainingId": 1}, {}, "kg")
            self.assertTrue(out["date"].startswith("2026-"), out["date"])


class Round18ServerSide(unittest.TestCase):
    def test_concurrent_partial_readiness_posts_all_survive(self):
        import threading
        import time as _time
        d = tempfile.mkdtemp(prefix="gym-ready-")
        real = features._load_readiness

        def slow(data_dir=None):
            out = real(data_dir)
            _time.sleep(0.05)  # widen the read-merge-write window so an unlocked implementation loses updates
            return out

        payloads = [{"whoop_recovery": 20}, {"sleep_hours": 8}, {"subjective": 3}, {"rhr_delta_bpm": 2}, {"acwr": 1.1}]
        gate = threading.Barrier(len(payloads))
        errors = []

        def post(payload):
            gate.wait()
            try:
                features.save_readiness(payload, d)
            except Exception as e:  # pragma: no cover
                errors.append(e)

        with mock.patch.object(features, "_load_readiness", slow):
            threads = [threading.Thread(target=post, args=(pl,)) for pl in payloads]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(errors, [])
        stored = real(d)
        for key in ("whoop_recovery", "sleep_hours", "subjective", "rhr_delta_bpm", "acwr"):
            self.assertIn(key, stored)

    def test_the_readiness_get_uses_per_field_freshness(self):
        import json as _json
        from datetime import datetime, timedelta, timezone
        d = tempfile.mkdtemp(prefix="gym-ready-")
        old = (datetime.now(timezone.utc) - timedelta(hours=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
        new = (datetime.now(timezone.utc) - timedelta(hours=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(features._readiness_path(d), "w") as f:
            _json.dump({"whoop_recovery": 20, "subjective": 4, "updated": new,
                        "field_updated": {"whoop_recovery": old, "subjective": new}}, f)
        fake = mock.Mock(data_dir=d, credentials={"token": "x"})
        with mock.patch.object(features, "_client", fake), mock.patch.dict(os.environ, {"DATA_DIR": d}):
            body = gym_app.app.test_client().get("/api/coach/readiness", headers=BASIC).get_json()
        self.assertEqual(body["adjustment"]["band"], "high")  # the 40 h old red recovery no longer counts
        self.assertFalse(body["stale"])

    def test_strict_library_needs_every_device_source(self):
        with mock.patch.dict(os.environ, {"DATA_DIR": tempfile.mkdtemp(prefix="gym-lib-")}):
            client = api_client.SpeedianceClient()
        client.save_config("1", "tok", "Global", 0, "", 2, True, [], [])  # Gym Pal + Monster moves
        client.library_cache = None

        def request(m, u, **kw):
            if "actionLibraryTab/list?deviceType=2" in u:
                return Resp(200, {"code": 0, "data": []})  # the whole Pal source answered with nothing
            if "actionLibraryTab/list?deviceType=1" in u:
                return Resp(200, {"code": 0, "data": [{"id": 9, "name": "M"}]})
            return Resp(200, {"code": 0, "data": [{"actionLibraryGroupList": [{"id": 5, "name": "Press"}]}]})

        client._request = request
        client.get_batch_details = lambda ids, strict=False: [{"id": i, "name": "x"} for i in ids]
        with self.assertRaises(api_client.UpstreamError):
            client.get_library(strict=True)
        self.assertFalse(os.path.exists(client.library_cache_file))

    def test_mixed_second_and_millisecond_timestamps_sort_by_real_time(self):
        older_ms, newer_s = 1790000000000, 1791000000
        recs = [{"type": 5, "trainingId": 1, "startTimestamp": older_ms}, {"type": 5, "trainingId": 2, "startTimestamp": newer_s}]
        client = mock.Mock()
        client.get_training_records.return_value = recs
        out = features._detailed_records(client, 30)
        self.assertEqual([r["trainingId"] for r in out], [2, 1])

    def test_integers_too_large_for_a_float_are_a_clean_400(self):
        import state_io
        for key in ("whoop_recovery", "sleep_hours", "acwr", "subjective", "rhr_delta_bpm"):
            with self.assertRaises(ValueError, msg=key):
                state_io.validate_readiness({key: 10 ** 400})
        fake = mock.Mock(data_dir=tempfile.mkdtemp(prefix="gym-body-"), credentials={"token": "x"})
        with mock.patch.object(features, "_client", fake), mock.patch.dict(os.environ, {"GYM_ALLOW_WRITES": "1"}):
            c = gym_app.app.test_client()
            for path, body in (("/api/coach/readiness", '{"whoop_recovery": %s}' % ("9" * 400)),
                               ("/api/coach/autoregulate", '{"whoop_recovery": %s}' % ("9" * 400)),
                               ("/api/coach/feedback", '{"rpe": %s}' % ("9" * 400)),
                               ("/api/coach/program", '{"readiness": {"whoop_recovery": %s}}' % ("9" * 400))):
                self.assertEqual(c.post(path, headers=BASIC, data=body, content_type="application/json").status_code, 400, path)


class Round18Harness(unittest.TestCase):
    def test_a_timed_exercise_is_not_planned_as_a_rep_count(self):
        sys.path.insert(0, os.path.dirname(__file__))
        import synth
        from datetime import date, datetime, timedelta
        from harness import planner
        from harness.model import Plan, Reading
        from harness.normalize import session_header, session_with_sets

        d = date(2026, 10, 26)
        newer = synth.history_record(11, synth.ts_ms(d.year, d.month, d.day - 3), title="Core")
        older = synth.history_record(12, synth.ts_ms(d.year, d.month, d.day - 10), title="Core")
        plank = synth.exercise("Synthetic Plank", [(0, 0, 60)], timed=True)
        plank["finishedReps"][0].update({"time": 60, "targetCount": 60, "finishedCount": 0})
        normalized = session_with_sets(newer, [plank])
        self.assertIsNone(normalized.sets[0].target)
        self.assertEqual(normalized.sets[0].seconds, 60)
        now = datetime(2026, 10, 26, 7, 0, tzinfo=synth.PT)
        result = planner.todays_plan(sessions=[normalized, session_header(older)],
                                     readings=[Reading("subjective", 4, "manual", now)], now=now)
        self.assertIsInstance(result, Plan)
        self.assertEqual(result.source, "user_template")
        plank_plan = next(e for e in result.exercises if e.name == "Synthetic Plank")
        self.assertIsNone(plank_plan.reps)  # 60 seconds is not 60 repetitions
        self.assertIn("timed exercise", plank_plan.load_basis)


class Round18bServerSide(unittest.TestCase):
    def test_a_config_from_before_the_flag_is_migrated_only_for_a_signed_in_imperial_account(self):
        import json as _json
        d = tempfile.mkdtemp(prefix="gym-cfg-")
        cases = (({"user_id": "42", "token": "t", "unit": 1}, True), ({"user_id": "42", "token": "t", "unit": 0}, None),
                 ({"user_id": "", "token": "", "unit": 1}, None), ({"user_id": "42", "token": "t", "unit": 1, "unit_confirmed": False}, False))
        for creds, expect in cases:
            with open(os.path.join(d, "config.json"), "w") as f:
                _json.dump(creds, f)
            with mock.patch.dict(os.environ, {"DATA_DIR": d}):
                client = api_client.SpeedianceClient()
            self.assertEqual(client.credentials.get("unit_confirmed"), expect, creds)

    def test_a_left_over_logged_out_imperial_config_does_not_confirm_the_next_account(self):
        client = client_with(1, False)
        client.credentials["user_id"] = ""
        client.save_config("B", "tok", "Global", 1, "", 1, False, [], [])
        self.assertFalse(api_client.unit_is_confirmed(client.credentials))

    def test_pain_without_an_exercise_is_refused_loudly(self):
        import state_io
        for payload in ({"pain": True}, {"pain": True, "note": "left shoulder"}):
            with self.assertRaises(ValueError) as ctx:
                state_io.validate_feedback(payload)
            self.assertIn("exercise", str(ctx.exception))
        self.assertEqual(state_io.validate_feedback({"pain": True, "exercise": "Row"})["exercise"], "Row")
        self.assertEqual(state_io.validate_feedback({"pain": False, "note": "fine"})["pain"], False)

    def test_avoid_matching_ignores_plurals_hyphens_and_case(self):
        import coach
        for entry, library in (("Walking lunges", "Walking Lunge"), ("lunges", "Walking Lunge"), ("Bulgarian split squats", "Bulgarian Split Squat"),
                               ("bulgarian split-squat", "Bulgarian Split Squat"), ("CHEST PRESSES", "Cable Chest Press"), ("Leg Curls", "Leg Curl (ankle)")):
            self.assertTrue(coach._is_avoided(library, {coach._norm_name(entry)}), (entry, library))
        self.assertFalse(coach._is_avoided("Lat Pulldown", {coach._norm_name("lunges")}))
        self.assertFalse(coach._is_avoided("Cable Press", {coach._norm_name("lat")}))  # under 4 characters never matches by part

    def test_a_repeat_pain_report_replaces_its_own_entry_instead_of_crowding_out_others(self):
        d = tempfile.mkdtemp(prefix="gym-fb-")
        features.log_feedback({"pain": True, "exercise": "Deadlift"}, d)
        for _ in range(600):
            features.log_feedback({"pain": True, "exercise": "Cable Chest Press"}, d)
            features.log_feedback({"avoid": "cable chest presses"}, d)
        self.assertIn("Deadlift", features._feedback_profile(d, days=3650)["avoid"])
        stored = json.load(open(features._feedback_path(d)))
        self.assertLessEqual(len([e for e in stored if e.get("pain") or e.get("avoid")]), 3)

    def test_the_coach_page_shows_the_safety_warnings(self):
        html = gym_app.app.test_client().get("/coach", headers=BASIC).get_data(as_text=True)
        for needle in ("avoid_unmatched", "feedback_error", "saving[i]"):
            self.assertIn(needle, html)

    def test_a_variant_that_is_not_the_exercises_own_is_refused(self):
        client = client_with(1, True)
        with self.assertRaises(ValueError) as ctx:
            client.prepare_workout("n", [ex(40, variant_id=987654)])
        self.assertIn("variant_id", str(ctx.exception))
        self.assertEqual(client.prepare_workout("n", [ex(40, variant_id=1007)])["actionLibraryList"][0]["actionLibraryId"], 1007)
        self.assertEqual(client.prepare_workout("n", [ex(40, variant_id=None)])["actionLibraryList"][0]["actionLibraryId"], 1007)

    def test_the_exercise_detail_route_reports_a_vendor_blip_not_an_empty_exercise(self):
        client = mock.Mock()
        client.credentials = {"token": "x"}
        client.get_exercise_detail.side_effect = api_client.UpstreamError("exercise detail failed (HTTP 503)")
        with mock.patch.object(gym_app, "client", client):
            resp = gym_app.app.test_client().get("/api/exercise/5", headers=BASIC)
        self.assertEqual(resp.status_code, 502)
        client.get_exercise_detail.assert_called_with(5, strict=True)

    def test_a_library_with_categories_but_no_exercises_is_an_outage(self):
        with mock.patch.dict(os.environ, {"DATA_DIR": tempfile.mkdtemp(prefix="gym-lib-")}):
            client = api_client.SpeedianceClient()
        client.save_config("1", "tok", "Global", 0, "", 1, False, [], [])
        client.library_cache = None
        client.get_categories = lambda strict=False: [{"id": 1, "name": "A"}]
        client._request = lambda m, u, **kw: Resp(200, {"code": 0, "data": []})
        with self.assertRaises(api_client.UpstreamError):
            client.get_library(strict=True)
        self.assertFalse(os.path.exists(client.library_cache_file))

    def test_mcp_save_workout_does_not_pass_a_vendor_rejection_off_as_success(self):
        client = client_with(1, True)
        tool = getattr(mcp_server.save_workout, "fn", mcp_server.save_workout)
        client._request = lambda m, u, **kw: Resp(200, {"code": 5000, "message": "Parameter Error"})
        with mock.patch.object(mcp_server, "_client", return_value=client), mock.patch.object(mcp_server, "_require_auth"), \
                mock.patch.dict(os.environ, {"GYM_ALLOW_WRITES": "1"}):
            self.assertIn("error", tool("n", [ex(40)]))
            client._request = lambda m, u, **kw: Resp(200, {"code": 0, "data": {"id": 1}})
            self.assertEqual(tool("n", [ex(40)])["code"], 0)

    def test_workout_session_validates_its_type_and_does_not_invent_a_header(self):
        client = mock.Mock()
        client.credentials = {"token": "x", "unit": 1, "unit_confirmed": True}
        client.get_training_detail.return_value = []
        tool = getattr(mcp_server.workout_session, "fn", mcp_server.workout_session)
        with mock.patch.object(mcp_server, "_client", return_value=client), mock.patch.object(mcp_server, "_require_auth"):
            self.assertIn("error", tool(1, "bogus"))
            out = tool(1, "Course")
        client.get_training_detail.assert_called_with(1, "course")
        self.assertEqual((out["type"], out["name"], out["calories"], out["duration"]), ("Program", None, None, None))

    def test_a_timed_set_adds_no_volume_and_every_set_needs_reps(self):
        client = client_with(1, True)
        timed = ex(20)
        timed["sets"] = [{"reps": 60, "weight": 20, "rest": 30, "unit": "sec"}, {"reps": 10, "weight": 20, "rest": 30, "unit": "reps"}]
        self.assertEqual(client.prepare_workout("n", [timed])["totalCapacity"], 200.0)
        for bad in ({"weight": 20, "rest": 30}, {"reps": None, "weight": 20}):
            broken = ex(20)
            broken["sets"] = [bad]
            with self.assertRaises(ValueError):
                client.prepare_workout("n", [broken])

    def test_plural_ies_words_match_their_singular(self):
        import coach
        self.assertTrue(coach._is_avoided("Reverse Fly", {coach._norm_name("reverse flies")}))
        self.assertEqual(coach._norm_name("Flies"), coach._norm_name("fly"))

    def test_a_huge_integer_in_an_apply_body_is_a_400_not_a_502(self):
        fake = mock.Mock(data_dir=tempfile.mkdtemp(prefix="gym-body-"), credentials={"token": "x", "unit": 1, "unit_confirmed": True})
        fake.get_library.return_value = []
        huge = "9" * 400
        with mock.patch.object(features, "_client", fake), mock.patch.object(features, "_authed", return_value=True), \
                mock.patch.object(features, "resolve_exercise", return_value={"groupId": 7, "matched_name": "Press", "confidence": "high"}):
            resp = gym_app.app.test_client().post(
                "/api/coach/apply", headers=BASIC, content_type="application/json",
                data='{"program": {"days": [{"exercises": [{"name": "a", "sets": 2, "reps": 5, "load": %s}]}]}, "load_unit": "kg"}' % huge)
        self.assertEqual(resp.status_code, 400)

    def test_no_page_sends_the_browser_to_a_get_login(self):
        root = os.path.join(os.path.dirname(__file__), "..", "templates")
        for name in os.listdir(root):
            text = open(os.path.join(root, name), encoding="utf-8").read()
            self.assertNotIn("href = '/login'", text, name)  # /login accepts POST only: a GET is a 405 page
            self.assertNotIn('href = "/login"', text, name)

    def test_a_non_finite_apply_body_is_a_400(self):
        fake = mock.Mock(data_dir=tempfile.mkdtemp(prefix="gym-body-"), credentials={"token": "x", "unit": 1, "unit_confirmed": True})
        with mock.patch.object(features, "_client", fake), mock.patch.object(features, "_authed", return_value=True):
            resp = gym_app.app.test_client().post("/api/coach/apply", headers=BASIC, content_type="application/json",
                                                  data='{"program": {"days": [{"exercises": [{"name": "a", "sets": 1e999}]}]}}')
        self.assertEqual(resp.status_code, 400)

    def test_the_unit_banner_shows_only_while_the_unit_is_unset(self):
        client = client_with(0, False)
        client.get_user_workouts = lambda: []
        with mock.patch.object(gym_app, "client", client):
            page = gym_app.app.test_client().get("/", headers=BASIC).get_data(as_text=True)
            settings = gym_app.app.test_client().get("/settings", headers=BASIC).get_data(as_text=True)
        self.assertIn("has not been set", page)
        self.assertNotIn("has not been set, so weights", settings)
        client = client_with(0, True)
        client.get_user_workouts = lambda: []
        with mock.patch.object(gym_app, "client", client):
            self.assertNotIn("has not been set", gym_app.app.test_client().get("/", headers=BASIC).get_data(as_text=True))


class Round19ServerSide(unittest.TestCase):
    LIB = [{"id": 1, "title": "Standing Triceps Extension", "mainMuscleGroupName": "Triceps", "isLeftRight": 0},
           {"id": 2, "title": "Leg Extension (ankle)", "mainMuscleGroupName": "Quads", "isLeftRight": 0},
           {"id": 3, "title": "Mystery Extension", "mainMuscleGroupName": "", "isLeftRight": 0}]

    def test_a_movement_that_trains_another_muscle_is_never_a_confident_match(self):
        only_triceps = [self.LIB[0]]
        for name in ("Leg extension (ankle)", "Leg extension"):
            match = features.resolve_exercise(only_triceps, name, "quads")
            self.assertEqual(match["confidence"], "low", name)
        self.assertEqual(features.resolve_exercise(self.LIB, "Leg extension (ankle)", "quads")["matched_name"], "Leg Extension (ankle)")
        self.assertEqual(features.resolve_exercise(self.LIB, "Leg extension (ankle)", "quads")["confidence"], "high")

    def test_an_unlabelled_library_exercise_is_at_most_a_medium_match(self):
        match = features.resolve_exercise([self.LIB[2]], "Mystery Extension", "quads")
        self.assertIn(match["confidence"], ("med", "low"))

    def test_the_importer_does_not_save_a_leg_load_onto_a_triceps_exercise(self):
        day = {"name": "D", "exercises": [{"name": "Leg extension (ankle)", "primary": "quads", "sets": 3, "reps": 10, "load": 60, "rest_sec": 60}]}
        exercises, resolved, unresolved = features.program_day_to_payload([self.LIB[0]], day, "lb", True)
        self.assertEqual((exercises, resolved), ([], []))
        self.assertIn("low-confidence", unresolved[0]["reason"])

    def test_a_repeat_report_keeps_the_other_exclusion_the_first_one_carried(self):
        d = tempfile.mkdtemp(prefix="gym-fb-")
        features.log_feedback({"pain": True, "exercise": "Cable Curl", "avoid": "Deadlift"}, d)
        features.log_feedback({"pain": True, "exercise": "Cable Curl", "avoid": "Squat"}, d)
        self.assertEqual(sorted(features._feedback_profile(d, days=3650)["avoid"]), ["Cable Curl", "Deadlift", "Squat"])
        features.log_feedback({"pain": True, "exercise": "Cable Curl", "avoid": "Deadlift"}, d)  # restating replaces the old one
        stored = json.load(open(features._feedback_path(d)))
        self.assertLessEqual(len(stored), 2)

    def test_a_vita_exercise_saved_without_its_stat_type_is_refused(self):
        client = client_with(1, True)
        client.get_batch_details = lambda ids, **kw: [{"id": i, "actionLibraryList": [{"id": i + 1000}], "dataStatType": 6} for i in ids]
        with self.assertRaises(ValueError) as ctx:
            client.prepare_workout("n", [ex(8)])
        self.assertIn("level", str(ctx.exception))
        payload = client.prepare_workout("n", [ex(8, data_stat_type=6)])
        self.assertEqual(payload["actionLibraryList"][0]["level"], "8")

    def test_the_editor_post_must_match_the_accounts_current_unit(self):
        client = client_with(0, True)
        calls = []
        client._request = lambda m, u, **kw: (calls.append(u), Resp(200, {"code": 0, "data": {"id": 1}}))[1]
        with mock.patch.object(gym_app, "client", client), mock.patch.dict(os.environ, {"GYM_ALLOW_WRITES": "1"}):
            c = gym_app.app.test_client()
            stale = c.post("/create", headers=BASIC, json={"name": "n", "unit": 1, "exercises": [ex(100)]}).get_json()
            self.assertEqual(stale["status"], "error")
            self.assertIn("unit setting changed", stale["message"])
            self.assertEqual(calls, [])
            ok = c.post("/create", headers=BASIC, json={"name": "n", "unit": 0, "exercises": [ex(40)]}).get_json()
            self.assertEqual(ok["status"], "success")

    def test_reps_must_be_whole_numbers_of_at_least_one(self):
        client = client_with(1, True)
        for bad in ("0", 0.5, 1.5, "abc", -3):
            broken = ex(10)
            broken["sets"] = [{"reps": bad, "weight": 10, "rest": 60}]
            with self.assertRaises(ValueError, msg=repr(bad)):
                client.prepare_workout("n", [broken])
        for good in ("10", 10, 10.0):
            fine = ex(10)
            fine["sets"] = [{"reps": good, "weight": 10, "rest": 60}]
            self.assertEqual(client.prepare_workout("n", [fine])["actionLibraryList"][0]["setsAndReps"], "10")

    def test_a_timed_preset_set_adds_no_volume_either(self):
        client = client_with(1, True)
        timed = ex(13, preset_id=1)
        timed["sets"] = [{"reps": 60, "weight": 13, "rest": 30, "unit": "sec"}]
        self.assertEqual(client.prepare_workout("n", [timed])["totalCapacity"], 0)


class Round19BatchB(unittest.TestCase):
    def mcp(self, name):
        return getattr(getattr(mcp_server, name), "fn", getattr(mcp_server, name))

    def test_a_readiness_that_names_no_known_signal_is_an_error_not_no_reading(self):
        client = mock.Mock(data_dir=tempfile.mkdtemp())
        red = {"whoop_recovery": 12, "sleep_hours": 4.5}
        with mock.patch.object(mcp_server, "_client", return_value=client), mock.patch.object(mcp_server, "_require_auth"), \
                mock.patch.object(mcp_server, "_fresh_readiness", return_value=red):
            for bad in ({}, {"recovery": 5}, {"whoop_recovery": 50, "recovry": 1}):
                self.assertIn("error", self.mcp("generate_program")(readiness=bad), bad)
                self.assertIn("error", self.mcp("autoregulate")(bad), bad)
            self.assertIsNotNone(self.mcp("generate_program")()["autoregulation"])  # the stored red reading still applies
            self.assertNotIn("error", self.mcp("generate_program")(readiness={"whoop_recovery": 80}))
        with mock.patch.object(features, "_fresh_readiness", return_value=red), mock.patch.object(features, "_client", mock.Mock(data_dir=tempfile.mkdtemp())):
            for bad in ({}, {"recovery": 5}):
                resp = gym_app.app.test_client().post("/api/coach/program", headers=BASIC, json={"readiness": bad})
                self.assertEqual((resp.status_code, resp.get_json()["error"]), (400, "invalid_readiness"), bad)

    def test_avoid_names_in_any_script_are_kept_and_reported(self):
        import coach
        for name in ("スクワット", "Übung", "Café Curl"):
            self.assertNotEqual(coach._norm_name(name), "", name)
        program = coach.generate_program(feedback={"avoid": ["スクワット"], "events": [1]})
        self.assertIn(coach._norm_name("スクワット"), program["avoid_unmatched"])

    def test_variant_and_preset_ids_are_refused_not_replaced(self):
        client = client_with(1, True)
        for key, bad in (("variant_id", "abc"), ("variant_id", -5), ("variant_id", 1.5), ("preset_id", 0.5), ("preset_id", -5),
                         ("preset_id", "x"), ("preset_id", True), ("preset_id", 99999)):
            broken = ex(10, **{key: bad})
            with self.assertRaises(ValueError, msg=(key, bad)):
                client.prepare_workout("n", [broken])
        for key, good in (("variant_id", 1007), ("variant_id", "1007"), ("variant_id", None), ("preset_id", -1), ("preset_id", "-1"),
                          ("preset_id", None), ("preset_id", 3)):
            client.prepare_workout("n", [ex(10, **{key: good})])

    def test_an_unknown_training_id_is_not_found_not_an_empty_session(self):
        client = client_with(1, True)
        for body in ({"code": 0, "data": None}, {"code": 0}, {"code": 0, "data": {}}):
            client._request = lambda m, u, body=body, **kw: Resp(200, body)
            with self.assertRaises(api_client.UpstreamError):
                client.get_training_detail("999", "custom")
        creds = mock.Mock()
        creds.credentials = {"token": "x", "unit": 1, "unit_confirmed": True}
        creds.get_training_detail.side_effect = api_client.UpstreamError("training detail not found")
        with mock.patch.object(mcp_server, "_client", return_value=creds), mock.patch.object(mcp_server, "_require_auth"):
            self.assertIn("error", self.mcp("workout_session")(999, "custom"))

    def test_the_dashboard_survives_opaque_template_ids(self):
        creds = {"token": "x", "user_id": "42", "unit": 0, "region": "Global", "device_type": 1,
                 "owned_accessories": [], "owned_devices": [], "custom_instruction": "", "allow_monster_moves": False}
        workouts = [{"id": 101, "code": "a", "title": "Numeric", "name": "Numeric", "totalCapacity": 1, "durationMinute": 1, "actionNum": 1},
                    {"id": "9f3ac2e8d1", "code": "b", "title": "Opaque", "name": "Opaque", "totalCapacity": 1, "durationMinute": 1, "actionNum": 1},
                    {"id": 7, "code": "c", "title": "Small", "name": "Small", "totalCapacity": 1, "durationMinute": 1, "actionNum": 1}]
        with mock.patch.object(gym_app.client, "credentials", creds), mock.patch.object(gym_app.client, "load_config", return_value=creds), \
                mock.patch.object(gym_app.client, "get_user_workouts", return_value=workouts), mock.patch.object(gym_app.client, "get_accessories", return_value=[]):
            page = gym_app.app.test_client().get("/", headers=BASIC).get_data(as_text=True)
        self.assertNotIn("Invalid token", page)
        for title in ("Numeric", "Opaque", "Small"):
            self.assertIn(title, page)
        self.assertLess(page.index("Small"), page.index("Numeric"))  # numeric ids keep their numeric order

    def test_a_vendor_outage_on_the_library_pages_says_so(self):
        creds = {"token": "x", "user_id": "42", "unit": 1, "unit_confirmed": True, "region": "Global", "device_type": 1,
                 "owned_accessories": [], "owned_devices": [], "custom_instruction": "", "allow_monster_moves": False}

        def down(*a, strict=False, **kw):  # like the real client: only the strict call reports the outage
            if strict:
                raise api_client.UpstreamError("exercise library unavailable")
            return []
        with mock.patch.object(gym_app.client, "credentials", creds), mock.patch.object(gym_app.client, "load_config", return_value=creds), \
                mock.patch.object(gym_app.client, "get_library", side_effect=down), mock.patch.object(gym_app.client, "get_categories", side_effect=down), \
                mock.patch.object(gym_app.client, "get_exercise_detail", side_effect=lambda *a, strict=False, **kw: down(strict=strict) or {}), \
                mock.patch.object(gym_app.client, "get_accessories", return_value=[]):
            c = gym_app.app.test_client()
            for path, words in (("/library", "Error loading library"), ("/create", "Could not load the exercise library")):
                page = c.get(path, headers=BASIC, follow_redirects=True).get_data(as_text=True)
                self.assertIn(words, page, path)
            resp = c.get("/exercise/5", headers=BASIC)
            self.assertEqual(resp.status_code, 302)  # not a 500 page


class Round20ServerSide(unittest.TestCase):
    def test_every_readiness_entry_point_refuses_a_typo(self):
        c = gym_app.app.test_client()
        with mock.patch.object(features, "_fresh_readiness", return_value={}), mock.patch.object(features, "_client", mock.Mock(data_dir=tempfile.mkdtemp())):
            for path in ("/api/coach/autoregulate", "/api/coach/program"):
                body = {"whoop_recover": 5} if "autoregulate" in path else {"readiness": {"whoop_recover": 5}}
                resp = c.post(path, headers=BASIC, json=body)
                self.assertEqual((resp.status_code, resp.get_json()["error"]), (400, "invalid_readiness"), path)
            self.assertEqual(c.post("/api/coach/autoregulate", headers=BASIC, json={}).status_code, 400)
        with mock.patch.dict(os.environ, {"GYM_ALLOW_WRITES": "1"}), mock.patch.object(features, "_client", mock.Mock(data_dir=tempfile.mkdtemp())):
            resp = c.post("/api/coach/readiness", headers=BASIC, json={"whoop_recover": 5, "sleep_hours": 8})
            self.assertEqual(resp.status_code, 400)  # the typo must not be dropped while the sleep reading is saved
            self.assertEqual(c.post("/api/coach/readiness", headers=BASIC, json={"whoop_recovery": 5, "sleep_hours": 8}).status_code, 200)

    def test_one_sided_training_is_never_balanced(self):
        def verdict(*names):
            session = {"date": "2026-10-20", "exercises": [{"name": n, "sets": [{"reps": 10, "weight": 50}] * 3} for n in names]}
            with mock.patch.object(features, "_detailed_records", return_value=[{"trainingId": 1}]), \
                    mock.patch.object(features, "fetch_session_detail", return_value=session), mock.patch.object(features, "_history", return_value=[]):
                return features._deep_insights()["balance"]
        self.assertEqual(verdict("Bench Press")["verdict"], "push_biased")
        self.assertEqual(verdict("Seated Row")["verdict"], "pull_biased")
        self.assertEqual(verdict("Bench Press", "Seated Row")["verdict"], "balanced")
        self.assertEqual(verdict("Leg Press")["verdict"], "no_data")

    def test_a_vendor_error_inside_http_200_is_not_a_session_detail(self):
        creds = {"token": "x", "user_id": "42", "unit": 1, "unit_confirmed": True, "region": "Global", "device_type": 1,
                 "owned_accessories": [], "owned_devices": [], "custom_instruction": "", "allow_monster_moves": False}
        with mock.patch.object(gym_app.client, "credentials", creds), mock.patch.object(gym_app.client, "load_config", return_value=creds):
            for body, status in (({"code": 5001, "message": "internal"}, 502), ({"code": 0, "data": {"x": 1}}, 200)):
                with mock.patch.object(gym_app.client, "_request", return_value=Resp(200, body)):
                    resp = gym_app.app.test_client().get("/api/workout_history/detail/5?type=5", headers=BASIC)
                self.assertEqual(resp.status_code, status, body)

    def test_the_editor_page_reports_a_library_outage(self):
        for down in (("library",), ("categories",)):
            self.editor_with_outage(down)

    def editor_with_outage(self, down):
        creds = {"token": "x", "user_id": "42", "unit": 1, "unit_confirmed": True, "region": "Global", "device_type": 1,
                 "owned_accessories": [], "owned_devices": [], "custom_instruction": "", "allow_monster_moves": False}

        def lib(strict=False, **kw):
            if strict:
                raise api_client.UpstreamError("library")
            return []
        with mock.patch.object(gym_app.client, "credentials", creds), mock.patch.object(gym_app.client, "load_config", return_value=creds), \
                mock.patch.object(gym_app.client, "get_workout_detail", return_value={"name": "n", "actionLibraryList": []}), \
                mock.patch.object(gym_app.client, "get_library", side_effect=lib if "library" in down else lambda **kw: []), \
                mock.patch.object(gym_app.client, "get_categories", side_effect=lib if "categories" in down else lambda **kw: []):
            resp = gym_app.app.test_client().get("/edit/abc", headers=BASIC, follow_redirects=True)
        self.assertIn("Error loading data", resp.get_data(as_text=True), down)


class Round21(unittest.TestCase):
    def stored(self, **signals):
        d = tempfile.mkdtemp(prefix="gym-r21-")
        features.save_readiness(signals, d)
        return d, features._readiness_path(d)

    def mcp(self, name):
        return getattr(getattr(mcp_server, name), "fn", getattr(mcp_server, name))

    def test_a_partial_reading_does_not_hide_a_fresh_red_one(self):
        d, path = self.stored(whoop_recovery=10)
        with mock.patch.object(features, "_readiness_path", side_effect=lambda dd=None: path), mock.patch.object(features, "_client", mock.Mock(data_dir=d)):
            out = gym_app.app.test_client().post("/api/coach/program", headers=BASIC, json={"goal": "muscle", "readiness": {"sleep_hours": 9}}).get_json()
            self.assertEqual(out["autoregulation"]["band"], "low")
            higher = gym_app.app.test_client().post("/api/coach/program", headers=BASIC, json={"goal": "muscle", "readiness": {"whoop_recovery": 90}}).get_json()
            self.assertNotEqual(higher["autoregulation"]["band"], "low")  # what the caller says about a signal wins
            alone = gym_app.app.test_client().post("/api/coach/program", headers=BASIC, json={"goal": "muscle", "readiness": {"sleep_hours": 9}, "use_stored_readiness": False}).get_json()
            self.assertNotEqual(alone["autoregulation"]["band"], "low")
        client = mock.Mock(data_dir=d)
        with mock.patch.object(mcp_server, "_client", return_value=client), mock.patch.object(mcp_server, "_require_auth"):
            self.assertEqual(self.mcp("generate_program")(readiness={"sleep_hours": 9})["autoregulation"]["band"], "low")

    def test_an_unreadable_readiness_file_is_reported_not_read_as_no_reading(self):
        d = tempfile.mkdtemp(prefix="gym-r21-")
        path = features._readiness_path(d)
        open(path, "w").write("{not json")
        with mock.patch.object(features, "_readiness_path", side_effect=lambda dd=None: path), mock.patch.object(features, "_client", mock.Mock(data_dir=d)):
            c = gym_app.app.test_client()
            resp = c.get("/api/coach/readiness", headers=BASIC)
            self.assertEqual((resp.status_code, resp.get_json()["error"]), (500, "readiness_unreadable"))
            self.assertIn("readiness_error", c.post("/api/coach/program", headers=BASIC, json={"goal": "muscle"}).get_json())
        client = mock.Mock(data_dir=d)
        with mock.patch.object(mcp_server, "_client", return_value=client), mock.patch.object(mcp_server, "_require_auth"):
            self.assertIn("readiness_error", self.mcp("generate_program")())
        open(path, "w").write("[1]")
        self.assertEqual(features._readiness_read_error(d), "NotAnObject")
        os.remove(path)
        self.assertIsNone(features._readiness_read_error(d))  # never reported anything: not an error

    def test_prescriptions_are_never_truncated(self):
        for bad in (2.9, "2.5", True, float("nan"), float("inf"), 10 ** 400):
            with self.assertRaises(ValueError, msg=repr(bad)):
                features._whole(bad, "sets", 20)
        for good in (3, 3.0, "3", None):
            self.assertIn(features._whole(good, "sets", 20), (0, 3))
        library = [{"id": 7, "title": "Cable Row", "mainMuscleGroupName": "Lats", "isLeftRight": 0}]
        for bad in ({"name": "Cable Row", "primary": "back", "sets": 2.9, "reps": 8}, {"name": "Cable Row", "primary": "back", "sets": 3, "reps": 8.9}):
            with self.assertRaises(ValueError):
                features.program_day_to_payload(library, {"name": "D", "exercises": [bad]})

    def test_a_repeat_report_keeps_the_session_data_of_the_one_it_replaces(self):
        d = tempfile.mkdtemp(prefix="gym-fb-")
        features.log_feedback({"pain": True, "exercise": "Bench press", "rpe": 9.5, "difficulty": "too_hard", "soreness": 5}, d)
        features.log_feedback({"avoid": "bench press"}, d)
        stored = json.load(open(features._feedback_path(d)))
        self.assertTrue(any(e.get("rpe") == 9.5 and e.get("difficulty") == "too_hard" for e in stored))
        d2 = tempfile.mkdtemp(prefix="gym-fb-")
        features.log_feedback({"avoid": "Dips"}, d2)
        features.log_feedback({"avoid": "dips"}, d2)
        self.assertEqual(len(json.load(open(features._feedback_path(d2)))), 1)  # a pure restatement still replaces

    def test_short_avoid_names_apply_as_whole_words(self):
        import coach
        program = coach.generate_program(goal="muscle", days_per_week=4, feedback={"avoid": ["row", "fly"], "events": [1]})
        names = [e["name"].lower().split() for d in program["days"] for e in d["exercises"]]
        self.assertFalse([n for n in names if "row" in n or "fly" in n])
        self.assertNotIn("row", program["avoid_unmatched"])

    def test_a_name_with_no_primary_is_still_checked_against_the_muscle_its_name_implies(self):
        library = [{"id": 1, "title": "Triceps Extension", "mainMuscleGroupName": "Triceps", "isLeftRight": 0}]
        self.assertEqual(features.resolve_exercise(library, "Leg extension")["confidence"], "low")
        library.append({"id": 2, "title": "Leg Extension", "mainMuscleGroupName": "Quads", "isLeftRight": 0})
        match = features.resolve_exercise(library, "Leg extension")
        self.assertEqual((match["groupId"], match["confidence"]), (2, "high"))

    def test_available_accessories_must_be_a_usable_list(self):
        import coach
        for bad in ("barbell", [], [1], "Handles"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                coach.generate_program(goal="muscle", days_per_week=3, available_accessories=bad)
        resp = gym_app.app.test_client().post("/api/coach/program", headers=BASIC, json={"goal": "muscle", "available_accessories": "barbell"})
        self.assertEqual(resp.status_code, 400)
        upper = coach.generate_program(goal="muscle", days_per_week=3, available_accessories=["HANDLES", " Rope "])
        lower = coach.generate_program(goal="muscle", days_per_week=3, available_accessories=["handles", "rope"])
        self.assertEqual(upper["days"], lower["days"])

    def test_the_planner_anchors_a_one_sided_lift_on_the_weaker_side(self):
        from harness import normalize, planner
        from harness.model import SetResult
        sets = [SetResult("Split Squat", reps=8, load=20.0, side="left"), SetResult("Split Squat", reps=8, load=40.0, side="right")] * 3
        self.assertEqual(planner.anchor_load(sets), 20.0)
        self.assertIn("left 20 vs right 40", planner.asymmetry_note(sets))
        self.assertIsNone(planner.asymmetry_note([SetResult("Press", reps=8, load=30.0)] * 3))
        self.assertEqual(planner.anchor_load([SetResult("Press", reps=8, load=30.0), SetResult("Press", reps=8, load=34.0)]), 32.0)
        from datetime import date
        day = date(2026, 10, 26)
        from harness.model import SessionResult
        session = SessionResult("1", date(2026, 10, 24), 1_792_000_000_000, "T", sets=tuple(sets))
        self.assertEqual(planner.last_logged("Split Squat", [session])[0], 20.0)
        auto = {"volume_pct": 0, "rir_min": 2, "band": "high", "intensity_pct": 100}
        plan = planner._from_template(session, auto, 10.0, day)
        self.assertLessEqual(plan[0].load, 22.0)  # built on the 20 side, never on the 30 average
        self.assertIn("anchored on the lower side", plan[0].load_basis)
        detail = [{"actionLibraryName": "Split Squat", "finishedReps": [
            {"finishedCount": 8, "leftRight": 1, "trainingInfoDetail": {"weights": [20.0]}},
            {"finishedCount": 8, "leftRight": 2, "trainingInfoDetail": {"weights": [40.0]}},
            {"finishedCount": 8, "trainingInfoDetail": {"weights": [30.0]}}]}]
        got = normalize.session_with_sets({"trainingId": 1, "startTimestamp": 1_790_000_000}, detail)
        self.assertEqual([x.side for x in got.sets], ["left", "right", None])


@unittest.skipUnless(shutil.which("node"), "node is required")
class TemplateHandlersRunUnderNode(unittest.TestCase):
    def test_the_real_editor_browse_and_calendar_handlers_pass(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        out = subprocess.run([shutil.which("node"), "--test", os.path.join(root, "tests", "templates-ui.test.mjs")],
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout[-2000:] + out.stderr[-500:])


if __name__ == "__main__":
    unittest.main()
