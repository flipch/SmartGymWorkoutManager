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
                mock.patch.object(gym_app.client, "get_user_workouts", return_value=[]), mock.patch.object(gym_app.client, "get_accessories", return_value=[]), \
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


class Round22(unittest.TestCase):
    def mcp(self, name):
        return getattr(getattr(mcp_server, name), "fn", getattr(mcp_server, name))

    def test_program_loads_land_on_the_editors_grid_in_every_unit_pair(self):
        for load, unit, imperial, expected in ((36.4, "lb", True, 36.0), (70.5, "lb", True, 70.0), (36.4, "kg", False, 36.5), (22.25, "kg", False, 22.0),
                                               (100, "lb", False, 45.5), (45, "kg", True, 99)):
            self.assertEqual(features._to_account_unit(load, unit, imperial), expected, (load, unit, imperial))
        library = [{"id": 7, "title": "Cable Row", "mainMuscleGroupName": "Lats", "isLeftRight": 0}]
        day = {"name": "D", "exercises": [{"name": "Cable Row", "primary": "back", "sets": 1, "reps": 8, "load": 56.6}]}
        exercises, _, _ = features.program_day_to_payload(library, day, "lb", True)
        self.assertEqual(exercises[0]["sets"][0]["weight"], 57.0)

    def test_a_plain_cable_exercise_cannot_claim_to_be_a_vita_one(self):
        client = client_with(1, True)
        client.get_batch_details = lambda ids, **kw: [{"id": i, "actionLibraryList": [{"id": i + 1000}], "dataStatType": 0} for i in ids]
        with self.assertRaises(ValueError) as ctx:
            client.prepare_workout("n", [ex(8, data_stat_type=6)])
        self.assertIn("not a level-based", str(ctx.exception))
        client.get_batch_details = lambda ids, **kw: [{"id": i, "actionLibraryList": [{"id": i + 1000}]} for i in ids]  # key absent: unknown
        client.prepare_workout("n", [ex(8, data_stat_type=6)])

    def test_the_builder_parses_exactly_what_the_validator_accepted(self):
        client = client_with(1, True)
        wobbly = ex(10, preset_id="5.0", data_stat_type="0.0")
        wobbly["sets"] = [{"reps": "10.0", "weight": 5, "rest": "", "mode": "2.0", "unit": "reps"}]
        out = client.prepare_workout("n", [wobbly])["actionLibraryList"][0]
        self.assertEqual((out["templatePresetId"], out["setsAndReps"], out["sportMode"], out["breakTime2"]), (5, "10", "2", "60"))
        for key, value in (("rest", 0.5), ("mode", 2.5), ("reps", 10.5)):
            broken = ex(10)
            broken["sets"] = [{"reps": 10, "weight": 5, "rest": 60, key: value}]
            with self.assertRaises(ValueError, msg=key):
                client.prepare_workout("n", [broken])
        with self.assertRaises(ValueError):
            client.prepare_workout("n", [ex(10, data_stat_type=6.5)])

    def test_the_schedule_routes_forward_only_what_they_validated(self):
        creds = {"token": "x", "user_id": "42", "unit": 1, "unit_confirmed": True, "region": "Global", "device_type": 1}
        sent = []
        with mock.patch.object(gym_app.client, "credentials", creds), mock.patch.object(gym_app.client, "load_config", return_value=creds), \
                mock.patch.dict(os.environ, {"GYM_ALLOW_WRITES": "1"}), \
                mock.patch.object(gym_app.client, "schedule_workout", side_effect=lambda *a: sent.append(a) or True), \
                mock.patch.object(gym_app.client, "schedule_course", side_effect=lambda *a: sent.append(a) or True):
            c = gym_app.app.test_client()
            for bad in (True, -1, 0, 1.5, [1], {"a": 1}):
                self.assertEqual(c.post("/api/schedule", headers=BASIC, json={"date": "2026-11-01", "templateCode": bad, "status": 1}).status_code, 400, bad)
                self.assertEqual(c.post("/api/schedule_course", headers=BASIC, json={"date": "2026-11-01", "courseId": bad, "status": 1}).status_code, 400, bad)
            raw = c.post("/api/schedule_course", data='{"date":"2026-11-01","courseId":NaN,"status":1}', content_type="application/json", headers=BASIC)
            self.assertEqual(raw.status_code, 400)
            self.assertEqual(sent, [])
            self.assertEqual(c.post("/api/schedule_course", headers=BASIC, json={"date": "2026-11-01", "courseId": 123, "status": 1}).status_code, 200)
            self.assertEqual(c.post("/api/schedule", headers=BASIC, json={"date": "2026-11-01", "templateCode": "ab_12", "status": 1}).status_code, 200)
            self.assertEqual(sent, [("2026-11-01", 123, 1), ("2026-11-01", "ab_12", 1)])

    def test_the_planner_proposes_loads_the_machine_can_be_set_to(self):
        from datetime import date
        from harness import planner
        from harness.model import SessionResult, SetResult
        day = date(2026, 10, 26)
        auto = {"volume_pct": 0, "rir_min": 2, "band": "high", "intensity_pct": 100}
        for unit, step in (("lb", 1.0), ("kg", 0.5)):
            sets = tuple(SetResult("Cable Row", reps=10, load=76.0, unit=unit, target=10) for _ in range(3))
            session = SessionResult("1", date(2026, 10, 24), 1_792_000_000_000, "T", sets=sets)
            self.assertEqual(planner._grid(sets), step)
            load = planner._from_template(session, auto, 10.0, day)[0].load
            self.assertEqual(load % step, 0, (unit, load))
        self.assertEqual(planner._round(76.5, 1.0), 76.0)
        import coach
        names = [e["name"] for d in coach.generate_program(goal="muscle", days_per_week=3)["days"] for e in d["exercises"]]
        for unit, step in (("lb", 1.0), ("kg", 0.5)):
            for last in (61.0, 77.0, 83.0, 91.0, 99.0):
                sets = tuple(SetResult(n, reps=30, load=last, unit=unit, target=10) for n in names for _ in range(3))
                session = SessionResult("1", date(2026, 10, 24), 1_792_000_000_000, "T", sets=sets)
                warnings = []
                _, plan = planner._from_catalog([session], auto, "muscle", 3, "intermediate", day, 10.0, warnings)
                loads = [x.load for x in plan if x.load is not None]
                self.assertTrue(loads)
                self.assertTrue(all(abs(l % step) < 1e-9 for l in loads), (unit, last, loads))
                from_template = [x.load for x in planner._from_template(session, auto, 10.0, day) if x.load is not None]
                self.assertTrue(all(abs(l % step) < 1e-9 for l in from_template), (unit, last, from_template))

    def test_a_mid_set_peak_survives_the_summary(self):
        s = features._summarize_set({"finishedCount": 9, "capacity": 100, "trainingInfoDetail": {"weights": [20, 60, 20]}}, False)
        self.assertEqual((s["weight"], s["weight_max"]), ([20, 20], 60))
        self.assertEqual(features._set_weight(s), 60)
        self.assertEqual(features._set_weight({"weight": [20, 20]}), 20)
        self.assertEqual(features._set_weight({"weight": 30}), 30)

    def test_mixed_reports_cannot_evict_a_standing_exclusion(self):
        d = tempfile.mkdtemp(prefix="gym-fb-")
        features.log_feedback({"avoid": "Squat"}, d)
        for _ in range(520):
            features.log_feedback({"pain": True, "exercise": "Curl", "rpe": 9}, d)
        profile = features._feedback_profile(d, days=3650)
        self.assertEqual(sorted(profile["avoid"]), ["Curl", "Squat"])
        self.assertLessEqual(len(json.load(open(features._feedback_path(d)))), 102)

    def test_autoregulate_does_not_hide_a_fresh_stored_red_reading(self):
        d = tempfile.mkdtemp(prefix="gym-r22-")
        features.save_readiness({"whoop_recovery": 10}, d)
        path = features._readiness_path(d)
        with mock.patch.object(features, "_readiness_path", side_effect=lambda dd=None: path):
            c = gym_app.app.test_client()
            self.assertEqual(c.post("/api/coach/autoregulate", headers=BASIC, json={"subjective": 5}).get_json()["band"], "low")
            self.assertNotEqual(c.post("/api/coach/autoregulate?use_stored=0", headers=BASIC, json={"subjective": 5}).get_json()["band"], "low")
        client = mock.Mock(data_dir=d)
        with mock.patch.object(mcp_server, "_client", return_value=client), mock.patch.object(mcp_server, "_require_auth"):
            self.assertEqual(self.mcp("autoregulate")(subjective=5)["band"], "low")
            self.assertNotEqual(self.mcp("autoregulate")(subjective=5, use_stored_readiness=False)["band"], "low")


class Round23(unittest.TestCase):
    def test_classification_reads_words_not_substrings(self):
        import coach
        for name, muscle in (("Triceps Kickback", "triceps"), ("Cable Glute Kickback", "glutes"), ("Narrow Grip Push-Up", "chest"),
                             ("Upright Row", "shoulders"), ("Seated Cable Row", "back"), ("Bent-Over Rows", "back")):
            self.assertEqual(coach.classify_exercise(name)[0], muscle, name)
        for name in ("Medicine Ball Throw", "Rowing Machine"):
            self.assertNotEqual(coach.classify_exercise(name)[0], "back", name)
        library = [{"id": 1, "title": "Cable Glute Kickback", "mainMuscleGroupName": "Glutes", "isLeftRight": 0},
                   {"id": 2, "title": "Triceps Kickback", "mainMuscleGroupName": "Triceps", "isLeftRight": 0}]
        self.assertEqual(features.resolve_exercise(library, "Triceps Kickback")["groupId"], 2)

    def test_a_drop_set_gives_no_e1rm_but_still_counts_for_the_pr(self):
        drop = lambda day: {"date": day, "exercises": [{"name": "Bench Press", "sets": [
            {"reps": 10, "weight": [100, 20], "weight_note": "1x100, 9x20", "weight_max": 100}]}]}
        steady = lambda day: {"date": day, "exercises": [{"name": "Bench Press", "sets": [{"reps": 10, "weight": 50}]}]}
        def run(*sessions):
            with mock.patch.object(features, "_detailed_records", return_value=[{"trainingId": i} for i, _ in enumerate(sessions)]), \
                    mock.patch.object(features, "fetch_session_detail", side_effect=list(sessions)), mock.patch.object(features, "_history", return_value=[]):
                return features._deep_insights()
        out = run(drop("2026-10-20"), drop("2026-10-21"))
        self.assertEqual(out["lifts"], [])
        self.assertEqual(out["prs"][0]["weight"], 100)
        self.assertEqual(len(run(steady("2026-10-20"), steady("2026-10-21"))["lifts"]), 1)  # a plain set still gives an e1RM

    def test_the_validator_and_the_builder_agree_on_odd_values(self):
        client = client_with(1, True)
        for key, value in (("rest", []), ("rest", {}), ("rest", float("inf"))):
            broken = ex(10)
            broken["sets"] = [{"reps": 10, "weight": 5, key: value}]
            with self.assertRaises(ValueError, msg=repr(value)):
                client.prepare_workout("n", [broken])
        with self.assertRaises(ValueError):
            client.prepare_workout("n", [ex(10, preset_id=float("inf"))])

    def test_pages_cannot_be_framed(self):
        resp = gym_app.app.test_client().get("/api/insights", headers=BASIC)
        self.assertEqual(resp.headers.get("X-Frame-Options"), "DENY")
        self.assertIn("frame-ancestors 'none'", resp.headers.get("Content-Security-Policy", ""))

    def test_a_detail_outage_is_a_502_and_a_variantless_exercise_is_a_message(self):
        with mock.patch.object(features, "_authed", return_value=True), mock.patch.object(features, "_detailed_records", return_value=[{"trainingId": 1}]), \
                mock.patch.object(features, "fetch_session_detail", side_effect=api_client.UpstreamError("down")):
            resp = gym_app.app.test_client().get("/api/last_workout", headers=BASIC)
        self.assertEqual((resp.status_code, resp.get_json()["error"]), (502, "fetch_failed"))
        creds = {"token": "x", "user_id": "42", "unit": 1, "unit_confirmed": True, "region": "Global", "device_type": 1}
        with mock.patch.object(gym_app.client, "credentials", creds), mock.patch.object(gym_app.client, "load_config", return_value=creds), \
                mock.patch.object(gym_app.client, "get_exercise_detail", return_value={"id": 5, "actionLibraryList": []}):
            resp = gym_app.app.test_client().get("/exercise/5", headers=BASIC)
        self.assertEqual(resp.status_code, 302)

    def test_a_failed_deep_load_is_named_in_the_note(self):
        with mock.patch.object(features, "_authed", return_value=True), mock.patch.object(features, "_history", return_value=[]), \
                mock.patch.object(features, "_deep_insights", side_effect=RuntimeError("x")):
            out = gym_app.app.test_client().get("/api/insights", headers=BASIC).get_json()
        self.assertEqual(out["deep_error"], "RuntimeError")
        self.assertIn("could not be loaded", out["data_note"])


class Round24(unittest.TestCase):
    def mcp(self, name):
        return getattr(getattr(mcp_server, name), "fn", getattr(mcp_server, name))

    def test_the_media_allowlist_means_the_same_host_to_every_parser(self):
        import media
        refused = ["https://192.168.1.1\\.speediance.com/x.mp4", "https://evil.com\\@cdn.speediance.com/x.mp4", "https://a.speediance.com:444/x.mp4",
                   "https://cdn.speediance.com /x.mp4", "https://cdn.speediance.com\t/x.mp4", "https://cdn.speediance.com\n/x.mp4",
                   "https://user@cdn.speediance.com/x.mp4", "https://cdn.speediance.com@evil.com/x.mp4", "http://cdn.speediance.com/x.mp4",
                   "https://cdn.speediance.com/x.exe", "https://\u0441dn.speediance.com/x.mp4", "https://127.0.0.1/x.mp4",
                   "https://evil.com/.speediance.com/x.mp4", "https://evil.com?.speediance.com/x.mp4", "https://evil.com#.speediance.com/x.mp4",
                   "https://evil.com\\.d1rwzqjevxdih5.cloudfront.net/x.mp4", "https://" + "a" * 3000 + ".speediance.com/x.mp4", None, 5]
        for url in refused:
            self.assertFalse(media.is_allowed_url(url), repr(url)[:70])
        for url in ("https://cdn.speediance.com/a/b.mp4", "https://CDN.Speediance.com:443/a.jpg?x=1", "https://d1rwzqjevxdih5.cloudfront.net/x.png"):
            self.assertTrue(media.is_allowed_url(url), url)

    def test_the_media_proxy_never_connects_for_a_smuggled_host(self):
        with mock.patch("requests.get") as get:
            for url in ("https://192.168.1.1\\.speediance.com/x.mp4", "https://evil.com\\@cdn.speediance.com/x.mp4"):
                resp = gym_app.app.test_client().get("/media_proxy", headers=BASIC, query_string={"url": url})
                self.assertNotEqual(resp.status_code, 200, url)
            get.assert_not_called()

    def test_an_exercises_own_goal_unit_is_enforced_on_the_save_path(self):
        client = client_with(1, True)
        del client.is_exercise_unilateral  # exercise the real strict read
        for method, selected, ok_unit, bad_unit in ((2, None, "sec", "reps"), (1, None, "reps", "sec"), (0, "1", "sec", "reps")):
            client.get_exercise_detail = lambda gid, strict=False, m=method, sel=selected: {"isLeftRight": 0, "completionMethod": m, "selectCompletionMethod": sel}
            good, bad = ex(10), ex(10)
            good["sets"] = [{"reps": 30, "weight": 5, "rest": 60, "unit": ok_unit}]
            bad["sets"] = [{"reps": 30, "weight": 5, "rest": 60, "unit": bad_unit}]
            client.prepare_workout("n", [good])
            with self.assertRaises(ValueError, msg=(method, selected)) as ctx:
                client.prepare_workout("n", [bad])
            self.assertIn(f"must be '{ok_unit}'", str(ctx.exception))
        client.get_exercise_detail = lambda gid, strict=False: {"isLeftRight": 0, "completionMethod": 5}
        with self.assertRaises(ValueError) as ctx:
            client.prepare_workout("n", [ex(10)])
        self.assertIn("calorie-goal", str(ctx.exception))
        client.get_exercise_detail = lambda gid, strict=False: {"isLeftRight": 0, "completionMethod": 3}
        client.prepare_workout("n", [ex(10)])  # no rule for an unknown method

    def test_autoregulate_reports_an_unreadable_stored_reading(self):
        d = tempfile.mkdtemp(prefix="gym-r24-")
        path = features._readiness_path(d)
        for content in ("{not json", "[1]", '"text"'):
            open(path, "w").write(content)
            with mock.patch.object(features, "_readiness_path", side_effect=lambda dd=None: path):
                c = gym_app.app.test_client()
                resp = c.post("/api/coach/autoregulate", headers=BASIC, json={"sleep_hours": 9})
                self.assertEqual((resp.status_code, resp.get_json()["error"]), (500, "readiness_unreadable"), content)
                self.assertEqual(c.post("/api/coach/autoregulate?use_stored=0", headers=BASIC, json={"sleep_hours": 9}).status_code, 200)
                self.assertEqual(c.get("/api/coach/readiness", headers=BASIC).status_code, 500)
                prog = c.post("/api/coach/program", headers=BASIC, json={"goal": "muscle", "readiness": {"sleep_hours": 9}})
                self.assertEqual(prog.status_code, 200, content)  # a wrong-shaped file never crashes the planner
                self.assertIn("readiness_error", prog.get_json())
            client = mock.Mock(data_dir=d)
            with mock.patch.object(mcp_server, "_client", return_value=client), mock.patch.object(mcp_server, "_require_auth"):
                self.assertIn("error", self.mcp("autoregulate")(sleep_hours=9))
                self.assertNotIn("error", self.mcp("autoregulate")(sleep_hours=9, use_stored_readiness=False))

    def test_the_machines_sidedness_beats_a_name_guess_in_working_sets(self):
        sets = [{"reps": 10, "weight": 20}] * 4
        self.assertEqual(features._working_sets({"name": "Cable Curl", "unilateral": True, "sets": sets}), 2.0)
        self.assertEqual(features._working_sets({"name": "Single-arm Row", "unilateral": False, "sets": sets}), 4)
        self.assertEqual(features._working_sets({"name": "Single-arm Row", "sets": sets}), 2.0)  # no flag: the name guess


class Round25(unittest.TestCase):
    def test_real_library_ids_above_a_trillion_are_accepted(self):
        for gid in (451_234_567_890_123, 2 ** 53 - 1, "451234567890123"):
            self.assertEqual(api_client._group_id(gid, "x"), int(gid))
        for gid in (2 ** 53, 10 ** 17, -5):
            with self.assertRaises(ValueError):
                api_client._group_id(gid, "x")

    def test_a_different_angle_is_never_a_confident_match(self):
        lib = [{"id": 1, "title": "Incline Cable Press", "mainMuscleGroupName": "chest", "isLeftRight": 0}]
        self.assertEqual(features.resolve_exercise(lib, "Decline Cable Press", "chest")["confidence"], "low")
        self.assertNotEqual(features.resolve_exercise(lib, "Incline Cable Press", "chest")["confidence"], "low")

    def test_more_press_and_raise_names_classify(self):
        import coach
        for name, muscle in (("Incline Cable Press", "chest"), ("Cable Chest Press", "chest"), ("Front Raise", "shoulders"),
                             ("Bradford Press", "shoulders")):
            self.assertEqual(coach.classify_exercise(name)[0], muscle, name)

    def test_the_avoid_list_ignores_spacing_and_hyphens(self):
        import coach
        self.assertTrue(coach._is_avoided("Lat Pull-Down", {coach._norm_name("lat pulldown")}))
        self.assertTrue(coach._is_avoided("Lat Pulldown", {coach._norm_name("lat pull down")}))
        self.assertTrue(coach._is_avoided("Wide Lat Pull-Down Machine", {coach._norm_name("lat pulldown")}))
        self.assertFalse(coach._is_avoided("Hip Thrust", {coach._norm_name("dip")}))

    def test_feedback_with_an_unknown_field_is_refused_and_named(self):
        import state_io
        with self.assertRaises(ValueError) as ctx:
            state_io.validate_feedback({"rpe": 7, "Pain": True})
        self.assertIn("Pain", str(ctx.exception))
        self.assertEqual(state_io.validate_feedback({"rpe": 7})["rpe"], 7)

    def test_an_unwritable_volume_is_not_reported_as_logged_out(self):
        self.assertTrue(issubclass(mcp_server.NotAuthenticated, PermissionError))
        def boom():
            raise PermissionError("read-only volume")
        def logged_out():
            raise mcp_server.NotAuthenticated("log in")
        self.assertNotEqual(mcp_server._safe(boom).get("error"), "not_authenticated")
        self.assertEqual(mcp_server._safe(logged_out)["error"], "not_authenticated")

    def test_the_planner_never_borrows_the_other_sides_load(self):
        from datetime import date
        from harness import planner
        from harness.model import SessionResult, SetResult
        one_side = [SetResult("Split Squat", reps=8, load=40.0, side="right"), SetResult("Split Squat", reps=0, load=20.0, side="left")] * 3
        self.assertIsNone(planner.anchor_load(one_side))
        self.assertTrue(planner.side_missing(one_side))
        both = [SetResult("Split Squat", reps=8, load=40.0, side="right"), SetResult("Split Squat", reps=8, load=20.0, side="left")]
        self.assertEqual(planner.anchor_load(both), 20.0)
        warm = [SetResult("Split Squat", reps=8, load=40.0, side="right"), SetResult("Split Squat", reps=0, load=10.0, side="right"),
                SetResult("Split Squat", reps=8, load=40.0, side="left")]
        self.assertEqual(planner.anchor_load(warm), 40.0)  # a set with no reps is not a performed load
        old = SessionResult("1", date(2026, 10, 20), 1_791_000_000_000, "T", sets=tuple(both * 3))
        new = SessionResult("2", date(2026, 10, 24), 1_792_000_000_000, "T", sets=tuple(one_side))
        self.assertIsNone(planner.last_logged("Split Squat", [old, new]))  # fail closed, not the older session
        auto = {"volume_pct": 0, "rir_min": 2, "band": "high", "intensity_pct": 100}
        out = planner._from_template(new, auto, 5.0, date(2026, 10, 26))
        self.assertIsNone(out[0].load)
        self.assertIn("one side", out[0].load_basis)

    def test_a_lighter_newer_session_lowers_the_template_anchor(self):
        from datetime import date
        from harness import planner
        from harness.model import SessionResult, SetResult
        heavy = SessionResult("1", date(2026, 10, 18), 1_790_000_000_000, "T", sets=tuple(SetResult("Cable Row", reps=10, load=60.0, target=10) for _ in range(3)))
        light = SessionResult("2", date(2026, 10, 24), 1_792_000_000_000, "Other", sets=tuple(SetResult("Cable Row", reps=10, load=40.0, target=10) for _ in range(3)))
        auto = {"volume_pct": 0, "rir_min": 2, "band": "high", "intensity_pct": 100}
        out = planner._from_template(heavy, auto, 5.0, date(2026, 10, 26), [heavy, light])
        self.assertLessEqual(out[0].load, 45.0)
        self.assertIn("newer session", out[0].load_basis)
        same = planner._from_template(heavy, auto, 5.0, date(2026, 10, 26), [heavy])
        self.assertGreaterEqual(same[0].load, 60.0)


class Round26(unittest.TestCase):
    AUTO = {"volume_pct": 0, "rir_min": 2, "band": "high", "intensity_pct": 100}

    def test_a_newer_half_done_one_sided_session_overrules_the_old_template(self):
        from datetime import date
        from harness import planner
        from harness.model import SessionResult, SetResult
        both = [SetResult("Split Squat", reps=8, load=60.0, side="left", target=8), SetResult("Split Squat", reps=8, load=60.0, side="right", target=8)] * 3
        template = SessionResult("1", date(2026, 10, 18), 1_790_000_000_000, "T", sets=tuple(both))
        half = SessionResult("2", date(2026, 10, 24), 1_792_000_000_000, "Other",
                             sets=(SetResult("Split Squat", reps=8, load=20.0, side="left"), SetResult("Split Squat", reps=0, load=20.0, side="right")))
        out = planner._from_template(template, self.AUTO, 5.0, date(2026, 10, 26), [template, half])
        self.assertIsNone(out[0].load)
        self.assertIn("newer session", out[0].load_basis)

    def test_a_lighter_session_that_missed_its_reps_does_not_progress(self):
        from datetime import date
        from harness import planner
        from harness.model import SessionResult, SetResult
        template = SessionResult("1", date(2026, 10, 18), 1_790_000_000_000, "T",
                                 sets=tuple(SetResult("Cable Row", reps=10, load=60.0, target=10) for _ in range(3)))
        missed = SessionResult("2", date(2026, 10, 24), 1_792_000_000_000, "Other",
                               sets=tuple(SetResult("Cable Row", reps=3, load=40.0, target=8) for _ in range(3)))
        out = planner._from_template(template, self.AUTO, 5.0, date(2026, 10, 26), [template, missed])
        self.assertLessEqual(out[0].load, 40.0)

    def test_a_saved_workouts_eccentric_sets_reach_the_critique(self):
        detail = {"actionLibraryList": [{"title": "Cable Curl", "setsAndReps": "10,10,10", "sportMode": "3,3,3"},
                                        {"title": "Cable Row", "setsAndReps": "10,10", "sportMode": "1,1"},
                                        {"title": "Fly", "setsAndReps": "10,10"}]}
        modes = [e["mode"] for e in features._coach_workout_to_exercises(detail)]
        self.assertEqual(modes, ["eccentric", "standard", "standard"])
        mixed = {"actionLibraryList": [{"title": "Cable Curl", "setsAndReps": "10,10,10", "sportMode": "1,3,1"}]}
        self.assertEqual(features._coach_workout_to_exercises(mixed)[0]["mode"], "eccentric")  # any eccentric set counts
        report = features.coach.critique_session([{**e, "pattern": None} for e in features._coach_workout_to_exercises(detail)]) \
            if hasattr(features.coach, "critique_session") else None
        if report is not None:
            self.assertTrue(any("eccentric" in str(w).lower() for w in report.get("warnings", [])) or report.get("score", 100) <= 100)

    def test_the_legacy_detail_route_refuses_a_200_without_data(self):
        client = client_with(0, True)
        for body in ({"code": 0, "data": None}, {"code": 0}, {"code": 0, "data": {}}):
            client._request = lambda m, u, body=body, **kw: Resp(200, body)
            with mock.patch.object(gym_app, "client", client):
                resp = gym_app.app.test_client().get("/api/workout_history/detail/7?type=5", headers=BASIC)
            self.assertEqual((resp.status_code, resp.get_json()["error"]), (502, "training_detail_not_found"), body)
        client._request = lambda m, u, **kw: Resp(200, {"code": 0, "data": {"x": 1}})
        with mock.patch.object(gym_app, "client", client):
            self.assertEqual(gym_app.app.test_client().get("/api/workout_history/detail/7?type=5", headers=BASIC).status_code, 200)

    def test_only_a_real_boolean_false_saves_from_mcp(self):
        import asyncio
        program = {"days": [{"exercises": []}]}
        def call(dry_run):
            with mock.patch.dict(os.environ, {"GYM_ALLOW_WRITES": "1"}), mock.patch.object(mcp_server, "_client", return_value=mock.Mock()), \
                    mock.patch.object(mcp_server, "_require_auth"), \
                    mock.patch.object(mcp_server, "apply_program", return_value={"ok": True}) as applied:
                try:
                    asyncio.run(mcp_server.mcp.call_tool("create_workout_from_program", {"program": program, "dry_run": dry_run}))
                except Exception:
                    pass
                return applied
        applied = call(True)
        applied.assert_called_once()  # the control: a real boolean reaches the saver
        self.assertTrue(applied.call_args.args[4])
        for bad in (0, "no", "off", "false", None):
            call(bad).assert_not_called()
        self.assertFalse(call(False).call_args.args[4])

    def test_template_ids_are_real_ids(self):
        for bad in (True, False, 0, "0", "-1", -5, "", "1,2", "../x", None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                api_client._check_template_id(bad)
        for good in (5, "5", "abc_12-X", "1234567890123"):
            api_client._check_template_id(good)
        client = client_with(1, True)
        for bad in (True, 0, "-1"):
            with self.assertRaises(ValueError):
                client.prepare_workout("n", [ex(8)], template_id=bad)
            with self.assertRaises(ValueError):
                client.delete_workout(bad)

    def test_fractional_levels_and_rms_are_refused_not_truncated(self):
        client = client_with(1, True)
        client.get_batch_details = lambda ids, **kw: [{"id": i, "actionLibraryList": [{"id": i + 1000}], "dataStatType": 6} for i in ids]
        with self.assertRaises(ValueError):
            client.prepare_workout("n", [ex(5.9, data_stat_type=6)])
        self.assertEqual(client.prepare_workout("n", [ex(5.0, data_stat_type=6)])["actionLibraryList"][0]["level"], "5")
        client.get_batch_details = lambda ids, **kw: [{"id": i, "actionLibraryList": [{"id": i + 1000}], "dataStatType": 0} for i in ids]
        for rm in (7.9, 0):
            with self.assertRaises(ValueError):
                client.prepare_workout("n", [ex(rm, preset_id=5)])

    def test_prefer_is_kept_and_vendor_code_90_is_a_lost_session(self):
        import state_io
        self.assertEqual(state_io.validate_feedback({"rpe": 5, "prefer": "Cable row"})["prefer"], "Cable row")
        client = client_with(0, True)
        client._request = api_client.SpeedianceClient._request.__get__(client)
        with mock.patch("requests.Session.request", return_value=Resp(200, {"code": 90, "message": "login"})), \
                mock.patch.object(client, "_get_headers", return_value={}):
            with self.assertRaises(Exception) as ctx:
                client.get_training_detail("1", "custom")
        self.assertEqual(str(ctx.exception), "Unauthorized")

    def test_bad_requests_are_400s(self):
        c = gym_app.app.test_client()
        client = client_with(0, True)
        with mock.patch.object(gym_app, "client", client):
            self.assertEqual(c.get("/api/history?start=abc&end=abc", headers=BASIC).status_code, 400)
        for days in ([{"exercises": ["x"]}], [{"exercises": [None]}], [{"exercises": "x"}], [{}]):
            with self.assertRaises(ValueError):
                features.apply_program(client, {"days": days}, None, None, True, "kg")
        with mock.patch.object(features, "_client", client), mock.patch.dict(os.environ, {"GYM_ALLOW_WRITES": "1"}):
            resp = c.post("/api/coach/apply", headers=BASIC, json={"program": {"days": [{"exercises": ["x"]}]}, "dry_run": True})
        self.assertEqual(resp.status_code, 400)

    def test_the_exercise_page_survives_a_variant_without_a_coach(self):
        html = open(os.path.join(os.path.dirname(__file__), "..", "templates", "exercise_detail.html")).read()
        self.assertNotIn("first_var.coach.avatar", html)
        self.assertNotIn("first_var.coach.name", html)


class Round27(unittest.TestCase):
    AUTO = {"volume_pct": 0, "rir_min": 2, "band": "high", "intensity_pct": 100}

    def test_logging_only_one_side_is_a_half_done_lift(self):
        from datetime import date
        from harness import planner
        from harness.model import SessionResult, SetResult
        left_only = [SetResult("Split Squat", reps=8, load=40.0, side="left", target=8)] * 3
        self.assertTrue(planner.side_missing(left_only))
        self.assertIsNone(planner.anchor_load(left_only))
        template = SessionResult("1", date(2026, 10, 20), 1_791_000_000_000, "T", sets=tuple(left_only))
        out = planner._from_template(template, self.AUTO, 5.0, date(2026, 10, 26))
        self.assertIsNone(out[0].load)
        both = left_only + [SetResult("Split Squat", reps=8, load=40.0, side="right", target=8)] * 3
        self.assertFalse(planner.side_missing(both))

    def test_a_reduced_one_sided_session_keeps_whole_pairs(self):
        from datetime import date
        from harness import planner
        from harness.model import SessionResult, SetResult
        sets = tuple(SetResult("Split Squat", reps=8, load=40.0, side=side, target=8) for side in ("left", "right") for _ in range(4))
        template = SessionResult("1", date(2026, 10, 20), 1_791_000_000_000, "T", sets=sets)
        out = planner._from_template(template, {**self.AUTO, "volume_pct": -40}, 5.0, date(2026, 10, 26))
        self.assertEqual(out[0].sets % 2, 0)
        self.assertGreaterEqual(out[0].sets, 2)

    def test_the_live_tracer_reads_the_sessions_newer_than_the_template(self):
        from datetime import datetime, timezone
        from harness import tracer
        day = lambda d: int(datetime(2026, 10, d, 12, tzinfo=timezone.utc).timestamp() * 1000)
        records = [{"trainingId": 1, "startTimestamp": day(10), "title": "Push", "type": 5},
                   {"trainingId": 2, "startTimestamp": day(20), "title": "Pull", "type": 5},
                   {"trainingId": 3, "startTimestamp": day(5), "title": "Legs", "type": 5}]
        fetched = []

        class Stub:
            def __init__(self, **kw): pass
            def history(self, a, b): return records
            def detail(self, tid, typ):
                fetched.append(tid)
                return []
        with mock.patch.object(tracer, "SpeedianceNano", Stub):
            tracer.load_live(datetime(2026, 10, 26, 7, tzinfo=timezone.utc), "UTC", 60, 1, "lb", "Push")
        self.assertEqual(sorted(fetched), [1, 2])  # the template, and the newer session; not the older Legs

    def test_negative_and_non_finite_loads_are_bad_history(self):
        from harness import interlocks, normalize
        from harness.model import ProposedExercise
        self.assertIsNone(normalize.set_load({"finishedCount": 8, "capacity": -320}))
        self.assertIsNone(normalize.set_load({"finishedCount": 8, "capacity": float("inf")}))
        self.assertIsNone(normalize.set_load({"finishedCount": -8, "capacity": 320}))
        self.assertEqual(normalize.set_load({"finishedCount": 8, "capacity": 320}), 40.0)
        self.assertEqual(normalize.set_load({"trainingInfoDetail": {"weights": [float("inf"), float("inf"), 20]}}), 20.0)
        for load, last in ((-44.0, -40.0), (float("nan"), 40.0), (float("inf"), float("inf")), (0.0, 40.0), (44.0, -40.0)):
            ex_ = ProposedExercise("Bench", 3, 8, 2, "standard", load, "x", last_load=last)
            self.assertTrue(any(v.code == "bad_load" for v in interlocks.validate_exercises([ex_])), (load, last))
        ok = ProposedExercise("Bench", 3, 8, 2, "standard", 42.0, "x", last_load=40.0)
        self.assertEqual(interlocks.validate_exercises([ok]), [])

    def test_recovery_with_undatable_history_is_unknown_not_rested(self):
        from datetime import date
        import insights_core
        got = insights_core.recovery_advice([], date(2026, 10, 26), records_seen=2)
        self.assertEqual(got["recommendation"], "unknown")
        self.assertEqual(insights_core.recovery_advice([], date(2026, 10, 26))["recommendation"], "train")  # truly no history

    def test_a_typo_in_the_program_request_is_an_error_not_a_different_program(self):
        import coach
        for kw in ({"goal": "strenth"}, {"experience": "beginer"}, {"days_per_week": 9}, {"days_per_week": 0}, {"days_per_week": "3"}, {"days_per_week": True}):
            with self.assertRaises(ValueError, msg=str(kw)):
                coach.generate_program(**kw)
        self.assertEqual(coach.generate_program(goal="strength", days_per_week=3, experience="beginner")["days_per_week"], 3)
        resp = gym_app.app.test_client().post("/api/coach/program", headers=BASIC, json={"goal": "strenth"})
        self.assertEqual(resp.status_code, 400)


class Round28(unittest.TestCase):
    def test_a_fly_is_not_a_press(self):
        lib = [{"id": 1, "title": "Cable Chest Fly", "mainMuscleGroupName": "chest", "isLeftRight": 0}]
        self.assertEqual(features.resolve_exercise(lib, "Cable Chest Press", "chest")["confidence"], "low")
        self.assertNotEqual(features.resolve_exercise(lib, "Cable Chest Flye", "chest")["confidence"], "low")
        self.assertEqual(features._movement_tokens("Lat Pull-Down"), features._movement_tokens("Lat Pulldown"))
        self.assertNotEqual(features._movement_tokens("Chest Press"), features._movement_tokens("Chest Fly"))
        self.assertEqual(features._movement_tokens("Chest Flye"), {"fly"})
        self.assertEqual(features._movement_tokens("Dumbbell Flies"), {"fly"})

    def test_a_timed_row_or_ski_set_has_seconds_not_reps(self):
        from harness import normalize
        record = {"trainingId": 1, "startTimestamp": 1_790_000_000_000, "title": "T"}
        for cm in (0, 2):
            detail = [{"actionLibraryName": "Row", "completionMethod": cm,
                       "finishedReps": [{"finishedCount": 60, "targetCount": 60, "time": 60, "capacity": 6000}]}]
            got = normalize.session_with_sets(record, detail).sets[0]
            self.assertEqual((got.reps, got.seconds, got.target), (None, 60, None), cm)
        detail = [{"actionLibraryName": "Press", "completionMethod": 1, "finishedReps": [{"finishedCount": 8, "targetCount": 8, "time": 40}]}]
        self.assertEqual(normalize.session_with_sets(record, detail).sets[0].reps, 8)

    def test_critique_reads_loose_arguments_or_refuses_them(self):
        import coach
        ex_ = [{"name": "Cable chest press", "sets": "3", "reps": "30"}]
        for kw in ({"goal": "strenght"}, {"mode": "monthly"}):
            with self.assertRaises(ValueError):
                coach.critique_workout(ex_, **kw)
        self.assertEqual(coach.critique_workout(ex_, "strength", "Weekly")["mode"], "weekly")
        self.assertTrue(coach.critique_workout(ex_, "strength", "session")["warnings"])  # '30' reps is off-target, like 30
        ecc = [{"name": "Cable curl", "sets": 4, "reps": 10, "mode": "Eccentric overload"}]
        self.assertTrue(any("Eccentric" in w for w in coach.critique_workout(ecc, "muscle")["warnings"]))
        resp = gym_app.app.test_client().post("/api/coach/critique", headers=BASIC, json={"exercises": ex_, "goal": "strenght"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("goal", resp.get_json()["message"])

    def test_a_callers_feedback_keeps_the_stored_pain_exclusions(self):
        profile = {"events": 1, "avoid": ["Cable chest press"], "unreadable": None, "avg_rpe": 7}
        with mock.patch.object(features, "_feedback_profile", return_value=profile):
            resp = gym_app.app.test_client().post("/api/coach/program", headers=BASIC, json={"feedback": {"avg_rpe": 5}})
            names = {e["name"].lower() for d in resp.get_json()["days"] for e in d["exercises"]}
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("cable chest press", names)
        self.assertIn("cable chest press", " ".join(resp.get_json().get("feedback_applied", {}).get("avoid", [])).lower())

    def test_the_library_cache_is_per_region(self):
        a = client_with(0, True)
        b = client_with(0, True)
        b.save_config("42", "vendor-token", "EU", 0, "", 1, False, [], [], unit_confirmed=True)
        self.assertNotEqual(os.path.basename(a._get_library_cache_file()), os.path.basename(b._get_library_cache_file()))
        self.assertIn("EU", b._get_library_cache_file())

    def test_a_long_program_name_is_refused_with_the_right_limit(self):
        program = {"days": [{"name": "A", "exercises": []}, {"name": "B", "exercises": []}]}
        with self.assertRaises(ValueError) as ctx:
            features.apply_program(None, program, None, "x" * 71, True, "kg")
        self.assertIn("1-70", str(ctx.exception))
        try:
            features.apply_program(None, program, 0, "x" * 71, True, "kg")
        except Exception as e:  # a single day keeps the full limit: any other failure is fine here
            self.assertNotIn("name must be", str(e))

    def test_a_hand_made_settings_post_is_not_a_500(self):
        client = client_with(0, True)
        c = gym_app.app.test_client()
        with mock.patch.object(gym_app, "client", client):
            self.assertEqual(c.post("/settings/accessories", headers=BASIC, data={"accessories": "abc"}).status_code, 302)
            self.assertEqual(c.post("/settings/owned_devices", headers=BASIC, data={"owned_devices": "abc"}).status_code, 302)

    def test_the_coach_switcher_survives_a_variant_without_a_coach(self):
        html = open(os.path.join(os.path.dirname(__file__), "..", "templates", "exercise_detail.html")).read()
        self.assertNotIn("{{ variant.coach.", html)


class Round29(unittest.TestCase):
    def call(self, name, args, client=None):
        import asyncio
        client = client or mock.Mock()
        with mock.patch.dict(os.environ, {"GYM_ALLOW_WRITES": "1"}), mock.patch.object(mcp_server, "_client", return_value=client), \
                mock.patch.object(mcp_server, "_require_auth"):
            try:
                return asyncio.run(mcp_server.mcp.call_tool(name, args))
            except Exception as e:  # a refused call may surface as an exception or as an error result
                return e

    def test_a_boolean_is_never_a_template_id_over_rpc(self):
        client = mock.Mock()
        client.save_workout.return_value = {"code": 0}
        self.call("save_workout", {"name": "n", "exercises": [], "template_id": True}, client)
        client.save_workout.assert_not_called()
        self.call("save_workout", {"name": "n", "exercises": [], "template_id": 5}, client)
        self.assertEqual(client.save_workout.call_args.args[2], 5)  # the control: a real id still goes through
        self.call("save_workout", {"name": "n", "exercises": [], "template_id": "abc_1"}, client)
        self.assertEqual(client.save_workout.call_args.args[2], "abc_1")

    def test_a_misspelled_argument_fails_the_call(self):
        client = mock.Mock()
        client.save_workout.return_value = {"code": 0}
        self.call("save_workout", {"name": "n", "exercises": [], "bogus": 1}, client)
        client.save_workout.assert_not_called()
        with mock.patch.object(coach_mod := __import__("coach"), "autoregulate", wraps=coach_mod.autoregulate) as auto:
            self.call("autoregulate", {"whoop_recoveryy": 10, "subjective": 5, "use_stored_readiness": False})
            auto.assert_not_called()
            self.call("autoregulate", {"whoop_recovery": 10, "subjective": 5, "use_stored_readiness": False})
            auto.assert_called_once()

    def test_every_spelling_of_fly_is_one_exercise_for_a_pain_report(self):
        import coach
        for reported in ("Chest flyes", "chest fly", "Chest Flye", "Chest flies"):
            self.assertTrue(coach._is_avoided("Cable Chest Fly", {coach._norm_name(reported)}), reported)
        self.assertFalse(coach._is_avoided("Cable Chest Press", {coach._norm_name("Chest flyes")}))

    def test_a_row_ski_timed_exercise_is_a_timer_in_the_summary(self):
        record = {"trainingId": 1, "startTimestamp": 1_790_000_000_000, "title": "T"}
        for cm, timer in ((0, True), (2, True), (1, False)):
            detail = [{"actionLibraryName": "Row", "completionMethod": cm, "finishedReps": [{"finishedCount": 0, "targetCount": 60, "time": 45}]}]
            self.assertEqual(features._summarize_session(record, detail, "kg")["exercises"][0]["timer"], timer, cm)


class Round30(unittest.TestCase):
    AUTO = {"volume_pct": 0, "rir_min": 2, "band": "high", "intensity_pct": 100}

    def test_plural_movement_words_still_count_as_the_movement(self):
        lib = [{"id": 1, "title": "Cable Chest Fly", "mainMuscleGroupName": "chest", "isLeftRight": 0}]
        for asked in ("Cable Chest Presses", "Cable Chest Press"):
            self.assertEqual(features.resolve_exercise(lib, asked, "chest")["confidence"], "low", asked)
        self.assertEqual(features._movement_tokens("Chest Presses"), {"press"})
        self.assertEqual(features._movement_tokens("Biceps Curls"), {"curl"})

    def test_a_two_handed_request_is_not_filled_by_a_one_sided_exercise(self):
        lib = [{"id": 1, "title": "Single Arm Cable Chest Press", "mainMuscleGroupName": "chest", "isLeftRight": 1}]
        self.assertEqual(features.resolve_exercise(lib, "Dual-Handle Cable Chest Press", "chest", False)["confidence"], "low")
        self.assertNotEqual(features.resolve_exercise(lib, "Single Arm Cable Chest Press", "chest", True)["confidence"], "low")
        self.assertNotEqual(features.resolve_exercise(lib, "One-Arm Cable Chest Press", "chest", False)["confidence"], "low")
        self.assertNotEqual(features.resolve_exercise(lib, "Cable Chest Press", "chest", True)["confidence"], "low")  # the caller said one-sided
        bilateral = [{"id": 2, "title": "Cable Chest Press", "mainMuscleGroupName": "chest", "isLeftRight": 0}]
        self.assertEqual(features.resolve_exercise(bilateral, "Single Arm Cable Chest Press", "chest", True)["confidence"], "low")

    def test_a_newer_session_at_the_same_load_that_missed_its_reps_stops_progression(self):
        from datetime import date
        from harness import planner
        from harness.model import SessionResult, SetResult
        template = SessionResult("1", date(2026, 10, 17), 1_790_000_000_000, "T", sets=tuple(SetResult("Bench", reps=10, load=100.0, target=10) for _ in range(3)))
        missed = SessionResult("2", date(2026, 10, 24), 1_792_000_000_000, "Other", sets=tuple(SetResult("Bench", reps=4, load=100.0, target=10) for _ in range(3)))
        out = planner._from_template(template, self.AUTO, 5.0, date(2026, 10, 26), [template, missed])
        self.assertLessEqual(out[0].load, 100.0)
        self.assertIn("not every set", out[0].load_basis)
        heavier = SessionResult("3", date(2026, 10, 25), 1_792_100_000_000, "Other", sets=tuple(SetResult("Bench", reps=10, load=120.0, target=10) for _ in range(3)))
        out = planner._from_template(template, self.AUTO, 5.0, date(2026, 10, 26), [template, heavier])
        self.assertGreaterEqual(out[0].load, 100.0)  # a heavier newer session does not lower the anchor

    def test_timed_sets_make_no_strength_estimate(self):
        from datetime import datetime
        def lifts(completion_method):
            recs, summaries = [], []
            for day in (20, 22):
                rec = {"trainingId": day, "startTimestamp": int(datetime(2026, 10, day).timestamp() * 1000), "title": "T", "type": 5}
                detail = [{"actionLibraryName": "Cable Row", "completionMethod": completion_method, "totalCapacity": 6000,
                           "finishedReps": [{"finishedCount": 10, "targetCount": 10, "time": 60, "capacity": 600,
                                             "trainingInfoDetail": {"weights": [60.0] * 10}}]}]
                recs.append(rec); summaries.append(features._summarize_session(rec, detail, "lb"))
            with mock.patch.object(features, "_detailed_records", return_value=recs), \
                    mock.patch.object(features, "fetch_session_detail", side_effect=summaries):
                return features._deep_insights()["lifts"]
        self.assertTrue(lifts(1))  # the control: a rep-based row gets an e1RM series
        self.assertEqual(lifts(2), [])  # seconds are not repetitions
        self.assertEqual(lifts(0), [])

class Round31(unittest.TestCase):
    AUTO = {"volume_pct": 0, "rir_min": 2, "band": "high", "intensity_pct": 100}

    def test_the_vendors_muscle_vocabulary_is_understood(self):
        for title, tag, asked, muscle in (("Barbell Hip Thrust", "Gluteus", "Barbell hip thrust", "glutes"), ("Cable Shrug", "Trapezius", "Cable shrug", "traps"),
                                          ("Seated Row", "Erector Spinae", "Seated row", "back")):
            lib = [{"id": 1, "title": title, "mainMuscleGroupName": tag, "isLeftRight": 0}]
            self.assertNotEqual(features.resolve_exercise(lib, asked, muscle)["confidence"], "low", tag)
        full = [{"id": 1, "title": "Burpee Thrust", "mainMuscleGroupName": "Full Body", "isLeftRight": 0}]
        self.assertEqual(features.resolve_exercise(full, "Burpee thrust", "glutes")["confidence"], "med")  # not a contradiction, not a confirmation

    def test_a_boolean_is_never_a_day_index_or_a_day_count_over_rpc(self):
        import asyncio
        client = mock.Mock(data_dir=tempfile.mkdtemp(prefix="gym-r31-"))

        def call(tool, args, patched, **kw):
            with mock.patch.dict(os.environ, {"GYM_ALLOW_WRITES": "1"}), mock.patch.object(mcp_server, "_client", return_value=client), \
                    mock.patch.object(mcp_server, "_require_auth"), mock.patch.object(mcp_server, patched, **kw) as spy:
                try:
                    asyncio.run(mcp_server.mcp.call_tool(tool, args))
                except Exception:
                    pass
                return spy
        program = {"days": [{"exercises": []}, {"exercises": []}]}
        call("create_workout_from_program", {"program": program, "day_index": 1}, "apply_program", return_value={"ok": True}).assert_called_once()  # control
        for bad in (True, False):
            call("create_workout_from_program", {"program": program, "day_index": bad}, "apply_program", return_value={"ok": True}).assert_not_called()
        spy = call("generate_program", {"days_per_week": 3}, "coach", wraps=mcp_server.coach)
        self.assertTrue(spy.generate_program.called)  # control
        spy = call("generate_program", {"days_per_week": True}, "coach", wraps=mcp_server.coach)
        self.assertFalse(spy.generate_program.called)

    def test_a_newer_session_with_no_completed_set_withholds_progression(self):
        from datetime import date
        from harness import planner
        from harness.model import SessionResult, SetResult
        template = SessionResult("1", date(2026, 10, 17), 1_790_000_000_000, "T", sets=tuple(SetResult("Bench", reps=10, load=100.0, target=10) for _ in range(3)))
        failed = SessionResult("2", date(2026, 10, 24), 1_792_000_000_000, "Other", sets=tuple(SetResult("Bench", reps=0, load=50.0, target=10) for _ in range(3)))
        out = planner._from_template(template, self.AUTO, 5.0, date(2026, 10, 26), [template, failed])
        self.assertIsNone(out[0].load)
        self.assertIn("no completed set", out[0].load_basis)

    def test_a_seconds_only_side_does_not_crash_the_planner(self):
        from datetime import date
        from harness import planner
        from harness.model import SessionResult, SetResult
        sets = (SetResult("Plank Row", reps=8, load=40.0, side="left", target=8), SetResult("Plank Row", reps=None, load=40.0, seconds=30, side="right"))
        template = SessionResult("1", date(2026, 10, 17), 1_790_000_000_000, "T", sets=sets * 3)
        out = planner._from_template(template, self.AUTO, 5.0, date(2026, 10, 26))
        self.assertIsNone(out[0].load)

    def test_a_pain_report_must_name_something_matchable(self):
        import state_io
        for bad in ("???", "\U0001F3CB\ufe0f", "\u2014", "\u200b\u200b", "__"):
            for key in ("exercise", "avoid"):
                with self.assertRaises(ValueError, msg=repr(bad)):
                    state_io.validate_feedback({"pain": True, key: bad})
        self.assertEqual(state_io.validate_feedback({"pain": True, "exercise": "Cable curl"})["exercise"], "Cable curl")

    def test_local_state_stays_out_of_git_and_docker(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        git = open(os.path.join(root, ".gitignore")).read().split()
        docker = open(os.path.join(root, ".dockerignore")).read().split()
        for pattern in ("coach_feedback.json*", "*.lock", "readiness.json"):
            self.assertIn(pattern, git)
            self.assertIn(pattern, docker)


class Round32(unittest.TestCase):
    AUTO = {"volume_pct": 0, "rir_min": 2, "band": "high", "intensity_pct": 100}

    def test_the_catalog_path_withholds_progression_after_a_failed_newest_session(self):
        from datetime import date
        from harness import planner
        from harness.model import SessionResult, SetResult
        old = SessionResult("1", date(2026, 10, 17), 1_790_000_000_000, "T", sets=tuple(SetResult("Cable chest press", reps=10, load=100.0, target=10) for _ in range(3)))
        failed = SessionResult("2", date(2026, 10, 24), 1_792_000_000_000, "O", sets=tuple(SetResult("Cable chest press", reps=0, load=50.0, target=10) for _ in range(3)))
        self.assertIsNone(planner.last_logged("Cable chest press", [old, failed]))
        self.assertEqual(planner.last_logged("Cable chest press", [old])[0], 100.0)

    def test_a_feedback_avoid_given_as_one_name_still_excludes_it(self):
        profile = {"events": 0, "avoid": [], "unreadable": None}
        with mock.patch.object(features, "_feedback_profile", return_value=profile):
            resp = gym_app.app.test_client().post("/api/coach/program", headers=BASIC, json={"feedback": {"avoid": "Cable fly"}})
        names = {e["name"].lower() for d in resp.get_json()["days"] for e in d["exercises"]}
        self.assertNotIn("cable fly", names)

    def test_zero_and_negative_ids_are_refused_as_strings_too(self):
        for bad in ("0", "-7", "-0", 0, -7, True):
            self.assertFalse(gym_app._record_arg(bad), repr(bad))
        for good in ("12", 12, "abc_1", "ab-12"):
            self.assertTrue(gym_app._record_arg(good), repr(good))

    def test_only_mapped_history_types_are_deep_exportable(self):
        html = open(os.path.join(os.path.dirname(__file__), "..", "templates", "history.html")).read()
        self.assertIn("(TYPE_MAP[r.type] || {}).detailType != null", html)


class Round33(unittest.TestCase):
    AUTO = {"volume_pct": 0, "rir_min": 2, "band": "high", "intensity_pct": 100}

    def test_an_impossible_completion_count_is_never_evidence(self):
        from harness import normalize
        record = {"trainingId": 1, "startTimestamp": 1_790_000_000_000, "title": "T"}
        for bad in (-1, float("nan"), float("inf"), True, "8", None):
            detail = [{"actionLibraryName": "Bench", "completionMethod": 1,
                       "finishedReps": [{"finishedCount": bad, "targetCount": 8, "capacity": 640, "trainingInfoDetail": {"weights": [80.0]}}]}]
            got = normalize.session_with_sets(record, detail).sets[0]
            self.assertIsNone(got.reps, repr(bad))
        detail = [{"actionLibraryName": "Bench", "completionMethod": 1, "finishedReps": [{"finishedCount": 8, "targetCount": float("inf"), "trainingInfoDetail": {"weights": [80.0]}}]}]
        got = normalize.session_with_sets(record, detail).sets[0]
        self.assertEqual((got.reps, got.target), (8, None))
        timed = [{"actionLibraryName": "Row", "completionMethod": 2, "finishedReps": [{"time": float("nan"), "trainingInfoDetail": {"weights": [80.0]}}]}]
        self.assertIsNone(normalize.session_with_sets(record, timed).sets[0].seconds)

    def test_a_timed_history_does_not_anchor_a_rep_prescription(self):
        from datetime import date, datetime, timezone
        from harness import planner
        from harness.model import Reading, SessionResult, SetResult
        timed = SessionResult("1", date(2026, 10, 24), 1_792_000_000_000, "Other", sets=tuple(SetResult("Cable chest press", reps=None, load=80.0, seconds=30) for _ in range(3)))
        now = datetime(2026, 10, 26, 7, tzinfo=timezone.utc)
        plan = planner.todays_plan(sessions=[timed], readings=[Reading("subjective", 4, "manual", now)], now=now, tz="UTC", days_per_week=1, goal="muscle")
        press = [e for e in plan.exercises if e.name.lower() == "cable chest press"]
        for e in press:
            self.assertIsNone(e.load)
            self.assertIn("timed", e.load_basis)


class Round34(unittest.TestCase):
    PROFILE = {"events": 1, "avoid": ["Cable chest press"], "unreadable": None, "avg_rpe": None}

    def program(self, body):
        with mock.patch.object(features, "_feedback_profile", return_value=self.PROFILE):
            resp = gym_app.app.test_client().post("/api/coach/program", headers=BASIC, json=body)
        names = {e["name"].lower() for d in (resp.get_json().get("days") or []) for e in d["exercises"]} if resp.status_code == 200 else set()
        return resp.status_code, names

    def test_a_callers_feedback_has_one_checked_shape(self):
        for bad in ([], False, "x", 5, {"avoid": [{"a": 1}]}, {"avoid": [["x"]]}, {"avoid": [1, "a"]}, {"avoid": 7}, {"avoid": {"a": 1}}):
            self.assertEqual(self.program({"feedback": bad})[0], 400, repr(bad))
        status, names = self.program({"feedback": {}})
        self.assertEqual(status, 200)
        self.assertNotIn("cable chest press", names)  # the stored exclusion still applies

    def test_a_string_avoid_is_one_name_even_without_stored_feedback(self):
        status, names = self.program({"feedback": {"avoid": "Cable fly"}, "use_stored_feedback": False})
        self.assertEqual(status, 200)
        self.assertNotIn("cable fly", names)
        status, names = self.program({"feedback": {"avoid": ["Cable fly"]}, "use_stored_feedback": False})
        self.assertNotIn("cable fly", names)

    def test_mcp_schedule_refuses_zero_and_negative_codes(self):
        client = mock.Mock()
        client.schedule_workout.return_value = True
        fn = getattr(mcp_server.schedule_workout, "fn", mcp_server.schedule_workout)
        with mock.patch.dict(os.environ, {"GYM_ALLOW_WRITES": "1"}), mock.patch.object(mcp_server, "_client", return_value=client), mock.patch.object(mcp_server, "_require_auth"):
            for bad in ("0", "-7", "-", "_", "1,2", "../x", ""):
                out = fn("2026-11-01", bad)
                self.assertIn("error", out, repr(bad))
            client.schedule_workout.assert_not_called()
            self.assertTrue(fn("2026-11-01", "abc_12")["ok"])
            self.assertTrue(fn("2026-11-01", "12345")["ok"])

    def test_docker_does_not_bake_in_caches(self):
        docker = open(os.path.join(os.path.dirname(__file__), "..", ".dockerignore")).read().split()
        for pattern in ("static/media_cache/", "library_cache*.json"):
            self.assertIn(pattern, docker)


class Round35(unittest.TestCase):
    def call(self, tool, args, patched, **kw):
        import asyncio
        client = mock.Mock(data_dir=tempfile.mkdtemp(prefix="gym-r35-"))
        with mock.patch.dict(os.environ, {"GYM_ALLOW_WRITES": "1"}), mock.patch.object(mcp_server, "_client", return_value=client), \
                mock.patch.object(mcp_server, "_require_auth"), mock.patch.object(mcp_server, patched, **kw) as spy:
            try:
                asyncio.run(mcp_server.mcp.call_tool(tool, args))
            except Exception:
                pass
            return spy

    def test_a_boolean_is_never_a_reading_or_an_rpe_over_rpc(self):
        self.call("log_feedback", {"rpe": 7}, "_persist_feedback", return_value={"rpe": 7}).assert_called_once()  # control
        self.call("log_feedback", {"rpe": 7.5, "soreness": 3}, "_persist_feedback", return_value={"rpe": 7.5}).assert_called_once()
        for args in ({"rpe": True}, {"soreness": True}, {"rpe": "7"}):
            self.call("log_feedback", args, "_persist_feedback", return_value={}).assert_not_called()
        self.call("set_readiness", {"whoop_recovery": 80}, "_persist_readiness", return_value={}).assert_called_once()
        for args in ({"whoop_recovery": True}, {"sleep_hours": True}, {"subjective": False}, {"acwr": True}):
            self.call("set_readiness", args, "_persist_readiness", return_value={}).assert_not_called()
        spy = self.call("autoregulate", {"whoop_recovery": 80, "use_stored_readiness": False}, "coach", wraps=mcp_server.coach)
        self.assertTrue(spy.autoregulate.called)
        for args in ({"whoop_recovery": True}, {"subjective": True}, {"acwr": True}, {"sleep_hours": True}, {"rhr_delta_bpm": True}):
            spy = self.call("autoregulate", {**args, "use_stored_readiness": False}, "coach", wraps=mcp_server.coach)
            self.assertFalse(spy.autoregulate.called, args)

    def test_a_one_rep_max_is_not_the_load_for_nine_reps(self):
        from datetime import date, datetime, timezone
        from harness import planner
        from harness.model import Reading, SessionResult, SetResult
        now = datetime(2026, 10, 26, 7, tzinfo=timezone.utc)

        def plan(reps):
            session = SessionResult("1", date(2026, 10, 24), 1_792_000_000_000, "Other", sets=tuple(SetResult("Cable chest press", reps=reps, load=100.0) for _ in range(3)))
            result = planner.todays_plan(sessions=[session], readings=[Reading("subjective", 4, "manual", now)], now=now, tz="UTC", days_per_week=1, goal="muscle")
            return next(e for e in result.exercises if e.name.lower() == "cable chest press")
        self.assertIsNotNone(plan(9).load)  # the control: a comparable rep count anchors
        self.assertIsNotNone(plan(6).load)
        self.assertIsNone(plan(1).load)
        self.assertIn("far from", plan(1).load_basis)

    def test_a_one_sided_exercise_without_side_metadata_is_not_bilateral(self):
        from datetime import date
        from harness import normalize, planner
        record = {"trainingId": 1, "startTimestamp": 1_790_000_000_000, "title": "T"}
        rows = [{"finishedCount": 8, "targetCount": 8, "trainingInfoDetail": {"weights": [20.0]}}, {"finishedCount": 8, "targetCount": 8, "trainingInfoDetail": {"weights": [60.0]}}]
        detail = [{"actionLibraryName": "Split Squat", "isLeftRight": 1, "completionMethod": 1, "finishedReps": rows * 3}]
        sets = normalize.session_with_sets(record, detail).sets
        self.assertEqual({x.side for x in sets}, {"unknown"})
        self.assertTrue(planner.side_missing(sets))
        self.assertIsNone(planner.anchor_load(sets))
        self.assertIsNone(planner.asymmetry_note(sets))
        from harness.model import SetResult
        self.assertIsNone(planner.asymmetry_note([SetResult("S", reps=8, load=20.0, side="left"), SetResult("S", reps=8, load=40.0, side="unknown")]))
        bilateral = [{**detail[0], "isLeftRight": 0}]
        self.assertEqual({x.side for x in normalize.session_with_sets(record, bilateral).sets}, {None})
        sided = [{**detail[0], "finishedReps": [{**r, "leftRight": 1 + i % 2} for i, r in enumerate(rows * 3)]}]
        self.assertEqual({x.side for x in normalize.session_with_sets(record, sided).sets}, {"left", "right"})

    def test_a_null_accessories_field_does_not_crash_the_exercise_page(self):
        src = open(os.path.join(os.path.dirname(__file__), "..", "app.py")).read()
        self.assertIn("str(detail.get('accessories') or '').split(',')", src)


@unittest.skipUnless(shutil.which("node"), "node is required")
class TemplateHandlersRunUnderNode(unittest.TestCase):
    def test_the_real_editor_browse_and_calendar_handlers_pass(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        out = subprocess.run([shutil.which("node"), "--test", os.path.join(root, "tests", "templates-ui.test.mjs")],
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout[-2000:] + out.stderr[-500:])


if __name__ == "__main__":
    unittest.main()


class Round36(unittest.TestCase):
    AUTO = {"volume_pct": 0, "rir_min": 2, "band": "high", "intensity_pct": 100}

    def plan(self, newer_sets):
        from datetime import date
        from harness import planner
        from harness.model import SessionResult, SetResult
        template = SessionResult("1", date(2026, 10, 18), 1_790_000_000_000, "T",
                                 sets=tuple(SetResult("Cable Row", reps=10, load=80.0, target=10) for _ in range(3)))
        newer = SessionResult("2", date(2026, 10, 24), 1_792_000_000_000, "Other", sets=tuple(newer_sets))
        return planner._from_template(template, self.AUTO, 5.0, date(2026, 10, 26), [template, newer])[0]

    def test_a_newer_session_far_from_the_prescribed_reps_does_not_progress(self):
        from harness.model import SetResult
        control = self.plan([SetResult("Cable Row", reps=10, load=80.0, target=10) for _ in range(3)])
        self.assertIsNotNone(control.load)  # a comparable newer session still counts
        self.assertGreater(control.load, 80.0)
        one_rep = self.plan([SetResult("Cable Row", reps=1, load=80.0, target=1) for _ in range(3)])
        self.assertLessEqual(one_rep.load, 80.0)  # 1 rep at a 1-rep target is not "hit every set" for a 10-rep prescription
        timed = self.plan([SetResult("Cable Row", reps=0, seconds=45, load=80.0) for _ in range(3)])
        self.assertIsNone(timed.load)
        self.assertIn("timed", timed.load_basis)

    def test_a_deeply_nested_body_is_not_a_server_error(self):
        import features
        self.assertTrue(features._all_finite({"a": [1.5, {"b": 2.0}]}))
        self.assertFalse(features._all_finite({"a": [1.0, {"b": float("inf")}]}))
        deep = []
        for _ in range(200_000):
            deep = [deep]
        self.assertTrue(features._all_finite(deep))  # recursive checking raised RecursionError here
        deep_bad = [float("nan")]
        for _ in range(200_000):
            deep_bad = [deep_bad]
        self.assertFalse(features._all_finite(deep_bad))


class Round37(unittest.TestCase):
    def test_plural_names_ending_in_ches_shes_xes_are_the_same_exercise(self):
        import coach
        for plural, single in (("Cable crunches", "Cable crunch"), ("Dumbbell benches", "Dumbbell bench"), ("Box squats", "Box squat"), ("Boxes", "Box")):
            self.assertEqual(coach._norm_name(plural), coach._norm_name(single), plural)
        base = coach.generate_program(goal="muscle", days_per_week=4)
        crunch = next(e["name"] for d in base["days"] for e in d["exercises"] if "crunch" in e["name"].lower())
        program = coach.generate_program(goal="muscle", days_per_week=4, feedback={"avoid": [crunch.lower() + "es" if crunch.lower().endswith("ch") else crunch.lower() + "es"]})
        self.assertFalse([e for d in program["days"] for e in d["exercises"] if "crunch" in e["name"].lower()])
        self.assertEqual(program["avoid_unmatched"], [])

    def test_a_different_side_upright_or_sumo_movement_is_not_a_high_confidence_match(self):
        lib = [{"id": 1, "title": "Single-Arm Upright Row", "mainMuscleGroupName": "trapezius", "isLeftRight": 1},
               {"id": 2, "title": "Cable Side Lunge", "mainMuscleGroupName": "quadriceps", "isLeftRight": 0},
               {"id": 3, "title": "Sumo Deadlift", "mainMuscleGroupName": "hamstrings", "isLeftRight": 0}]
        for name, muscle in (("Single-arm row", "back"), ("Lunge", "quads"), ("Deadlift", "hamstrings")):
            self.assertEqual(features.resolve_exercise(lib, name, muscle, name.startswith("Single"))["confidence"], "low", name)
        self.assertNotEqual(features.resolve_exercise(lib, "Cable Side Lunge", "quads")["confidence"], "low")  # the control: its own name

    def test_heavier_timed_only_newer_evidence_does_not_progress_a_rep_prescription(self):
        from datetime import date
        from harness import planner
        from harness.model import SessionResult, SetResult
        template = SessionResult("1", date(2026, 10, 18), 1_790_000_000_000, "T", sets=tuple(SetResult("Cable Row", reps=10, load=80.0, target=10) for _ in range(3)))
        for load in (79.0, 80.0, 81.0, 100.0):
            timed = SessionResult("2", date(2026, 10, 24), 1_792_000_000_000, "Other", sets=tuple(SetResult("Cable Row", reps=0, seconds=45, load=load) for _ in range(3)))
            out = planner._from_template(template, {"volume_pct": 0, "rir_min": 2, "band": "high", "intensity_pct": 100}, 5.0, date(2026, 10, 26), [template, timed])
            self.assertIsNone(out[0].load, load)

    def test_an_unreadable_feedback_file_is_not_quarantined_as_corrupt(self):
        import features
        d = tempfile.mkdtemp(prefix="gym-r37-")
        features.log_feedback({"pain": True, "exercise": "Cable curl"}, d)
        path = features._feedback_path(d)
        os.chmod(path, 0)
        try:
            if os.access(path, os.R_OK):
                self.skipTest("running as a user that ignores file modes")
            with self.assertRaises(OSError):
                features.log_feedback({"rpe": 7}, d)
            self.assertEqual(features._feedback_read_error(d), "PermissionError")
            self.assertEqual([n for n in os.listdir(d) if "corrupt" in n], [])
        finally:
            os.chmod(path, 0o600)
        self.assertIn("Cable curl", features._feedback_profile(d)["avoid"])


class Round38(unittest.TestCase):
    AUTO = {"volume_pct": 0, "rir_min": 2, "band": "high", "intensity_pct": 100}

    def test_a_drop_set_has_no_single_load_to_anchor_on(self):
        from harness.normalize import set_load
        row = lambda w: {"finishedCount": len(w), "capacity": sum(w), "trainingInfoDetail": {"weights": w}}
        self.assertEqual(set_load(row([60.0] * 10)), 60.0)  # the controls
        self.assertEqual(set_load(row([40.0, 40.0, 40.0, 20.0, 60.0])), 40.0)  # chains/eccentric swings keep the median
        self.assertIsNone(set_load(row([60.0] * 6 + [20.0] * 4)))  # six reps at 60 then four at 20 is not ten reps at 60

    def test_a_ramp_is_planned_from_the_set_at_the_working_load(self):
        from datetime import date
        from harness import planner
        from harness.model import SessionResult, SetResult
        ramp = SessionResult("1", date(2026, 10, 24), 1_792_000_000_000, "T", sets=(
            SetResult("Cable Row", reps=12, load=20.0, target=12), SetResult("Cable Row", reps=10, load=40.0, target=10),
            SetResult("Cable Row", reps=6, load=80.0, target=6)))
        out = planner._from_template(ramp, self.AUTO, 5.0, date(2026, 10, 26))[0]
        self.assertEqual(out.reps, 10)  # the 40 lb set's target, not the first set's 12

    def test_ab_work_with_a_straight_arm_or_tricep_rope_in_its_name_is_ab_work(self):
        import coach
        for title in ("Straight Arm Barbell Crunch", "Dual-Handle Straight-Arm Reverse Crunch", "Kneeling Tricep Rope Crunch",
                      "Standing Tricep Rope High-to-Low Woodchops", "Seated Tricep Rope Cross-Body Crunch"):
            self.assertEqual(coach.classify_exercise(title)[:2], ("abs", "core"), title)
        self.assertEqual(coach.classify_exercise("Straight Arm Pulldown")[0], "back")  # the control
        self.assertEqual(coach.classify_exercise("Tricep Rope Pushdown")[0], "triceps")


class Round39(unittest.TestCase):
    AUTO = {"volume_pct": 0, "rir_min": 2, "band": "high", "intensity_pct": 100}

    def drop_sessions(self):
        from datetime import date
        from harness.model import SessionResult, SetResult
        old = SessionResult("1", date(2026, 10, 18), 1_790_000_000_000, "T", sets=tuple(SetResult("Cable Row", reps=10, load=80.0, target=10) for _ in range(3)))
        drop = SessionResult("2", date(2026, 10, 24), 1_792_000_000_000, "Other", sets=tuple(SetResult("Cable Row", reps=10, load=None, target=10) for _ in range(3)))
        return old, drop

    def test_a_newer_session_without_a_usable_load_withholds_the_old_progression(self):
        from datetime import date
        from harness import planner
        old, drop = self.drop_sessions()
        out = planner._from_template(old, self.AUTO, 5.0, date(2026, 10, 26), [old, drop])
        self.assertIsNone(out[0].load)
        self.assertIsNone(planner.last_logged("Cable Row", [old, drop]))  # the catalog path agrees
        skipped = drop.__class__(drop.source_id, drop.local_day, drop.started_at_ms, drop.title,
                                 sets=tuple(s.__class__(s.exercise, reps=None, load=None) for s in drop.sets))
        self.assertIsNotNone(planner.last_logged("Cable Row", [old, skipped]))  # an exercise that was not done does not count

    def test_negative_or_fractional_set_counts_cannot_cancel_real_volume(self):
        import coach
        for sets in (-12, 2.5, "x", float("inf"), 101):
            with self.assertRaises(ValueError, msg=sets):
                coach.critique_workout([{"name": "Cable Bench Press", "sets": 12}, {"name": "Chest Fly", "sets": sets}])
        with self.assertRaises(ValueError):
            coach.critique_workout([{"name": "Bench Press", "sets": 3, "reps": -5}])
        self.assertTrue(coach.critique_workout([{"name": "Bench Press", "sets": "3", "reps": "8"}]))  # a client that sends strings still works

    def test_a_bad_month_deep_json_and_a_boolean_session_id_are_refused(self):
        import asyncio
        client = api_client.SpeedianceClient()
        for bad in ("2026-13", "../../x?y=1", "", "2026-1"):
            with self.assertRaises(ValueError, msg=bad):
                client.get_calendar_month(bad)
        resp = gym_app.app.test_client().post("/api/coach/critique", headers=BASIC, data="[" * 200000, content_type="application/json")
        self.assertEqual(resp.status_code, 400)
        spy = mock.Mock()
        with mock.patch.dict(os.environ, {"GYM_ALLOW_WRITES": "1"}), mock.patch.object(mcp_server, "_client", return_value=spy), mock.patch.object(mcp_server, "_require_auth"):
            for bad in (True, 0, -5, "7"):
                try:
                    asyncio.run(mcp_server.mcp.call_tool("workout_session", {"training_id": bad}))
                except Exception:
                    pass
        spy.get_training_detail.assert_not_called()

    def test_an_even_ramp_anchors_on_a_load_that_was_lifted(self):
        from datetime import date
        from harness import planner
        from harness.model import SessionResult, SetResult
        self.assertEqual(planner._lifted_median([30.0, 34.0]), 32.0)  # close loads still average
        self.assertEqual(planner._lifted_median([10.0, 100.0]), 10.0)  # far-apart loads do not
        ramp = SessionResult("1", date(2026, 10, 24), 1_792_000_000_000, "T", sets=(
            SetResult("Cable Row", reps=20, load=10.0, target=20), SetResult("Cable Row", reps=1, load=100.0, target=1)))
        out = planner._from_template(ramp, {"volume_pct": 0, "rir_min": 2, "band": "high", "intensity_pct": 100}, 5.0, date(2026, 10, 26))[0]
        self.assertLess(out.load, 15.0)  # not 56

    def test_a_partial_readiness_post_does_not_overwrite_an_unreadable_stored_reading(self):
        import features
        d = tempfile.mkdtemp(prefix="gym-r40-")
        features.save_readiness({"whoop_recovery": 10}, d)
        path = features._readiness_path(d)
        os.chmod(path, 0)
        try:
            if os.access(path, os.R_OK):
                self.skipTest("running as a user that ignores file modes")
            with self.assertRaises(OSError):
                features.save_readiness({"sleep_hours": 9}, d)
        finally:
            os.chmod(path, 0o600)
        self.assertEqual(features._load_readiness(d)["whoop_recovery"], 10)  # still there
        with open(path, "w") as f:
            f.write("{not json")
        features.save_readiness({"sleep_hours": 9}, d)  # a damaged file is moved aside, then the post is stored
        self.assertTrue([n for n in os.listdir(d) if ".corrupt-" in n])

    def test_an_accessories_answer_that_is_not_a_list_of_objects_is_empty(self):
        client = api_client.SpeedianceClient()
        for data, expected in ((None, []), ("x", []), ({"a": 1}, []), ([1, "y", {"id": 1}], [{"id": 1}]), ([], [])):
            with mock.patch.object(client, "_request", return_value=mock.Mock(json=lambda data=data: {"code": 0, "data": data})):
                self.assertEqual(client.get_accessories(), expected, data)

    def test_a_ramp_that_starts_heavy_takes_its_reps_from_the_lifted_load(self):
        from datetime import date
        from harness import planner
        from harness.model import SessionResult, SetResult
        ramp = SessionResult("1", date(2026, 10, 24), 1_792_000_000_000, "T", sets=(
            SetResult("Cable Row", reps=1, load=100.0, target=1), SetResult("Cable Row", reps=20, load=10.0, target=20)))
        out = planner._from_template(ramp, {"volume_pct": 0, "rir_min": 2, "band": "high", "intensity_pct": 100}, 5.0, date(2026, 10, 26))[0]
        self.assertEqual(out.reps, 20)  # the target of the 10 lb set that the anchor is, not the first set's single rep


class Round41(unittest.TestCase):
    def test_a_timed_library_entry_is_reported_and_the_rest_of_the_program_still_saves(self):
        def resolve(library, name, primary, uni=None):
            return {"groupId": 2 if name == "Cable crunch" else 7, "matched_name": name, "muscle": "x", "confidence": "high"}

        def prepare(nm, exercises):
            for ex in exercises:
                if ex["groupId"] == 2:
                    raise ValueError("exercise 2: this exercise is timed; every set's unit must be 'sec'")
        client = mock.Mock(data_dir=tempfile.mkdtemp(prefix="gym-r41-"), credentials={"token": "x", "unit": 1})
        client.get_library.return_value = []
        client.prepare_workout.side_effect = prepare
        client.save_workout.return_value = {"code": 0, "data": {"id": 1}}
        program = {"goal_label": "G", "days": [{"name": "D", "exercises": [
            {"name": "Press", "primary": "chest", "sets": 2, "reps": 8, "load": 0, "rest_sec": 60},
            {"name": "Cable crunch", "primary": "abs", "sets": 3, "reps": 12, "load": 0, "rest_sec": 60}]}]}
        with mock.patch.object(features, "resolve_exercise", side_effect=resolve):
            out = features.apply_program(client, program, 0, None, dry_run=False)
            self.assertEqual((out["created"], out["failed"]), (1, 0))
            day = out["results"][0]
            self.assertEqual(day["exercise_count"], 1)
            self.assertEqual([r["coach_name"] for r in day["resolved"]], ["Press"])
            self.assertEqual([u["name"] for u in day["unresolved"]], ["Cable crunch"])
            self.assertEqual([e["groupId"] for e in client.save_workout.call_args[0][1]], [7])
            client.prepare_workout.side_effect = ValueError("exercise 7: not found in the exercise library")  # any OTHER problem still aborts
            with self.assertRaises(ValueError):
                features.apply_program(client, program, 0, None, dry_run=False)

    def test_a_template_id_must_be_a_real_integer_or_an_opaque_id(self):
        for good in (1, 7, "1001", "abc_def-1", 2 ** 53 - 1):
            api_client._check_template_id(good)
        for bad in (True, 0, -1, 1.0, 1.5, float("nan"), float("inf"), 1e-05, 12345678901234567890, "12345678901234567890", "", None, "1,2"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                api_client._check_template_id(bad)


class Round42(unittest.TestCase):
    def test_only_a_real_true_confirms_the_unit(self):
        for good in (True,):
            self.assertTrue(api_client.unit_is_confirmed({"unit_confirmed": good}))
        for bad in ("false", "true", 1, "yes", [1], {}, None, 0, False):
            self.assertFalse(api_client.unit_is_confirmed({"unit_confirmed": bad}), repr(bad))

    def test_non_ascii_digits_are_not_ids_or_months(self):
        client = api_client.SpeedianceClient()
        with self.assertRaises(ValueError):
            client.get_calendar_month("２０２６-10")
        client.get_calendar_month  # the ASCII control is exercised by the earlier month test
        for bad in ("١", "²", "1_0", "１２"):
            with self.assertRaises(ValueError, msg=bad):
                api_client._group_id(bad, "x")
        for bad in ("١", "²", "１２"):  # "1_0" is a legal opaque template id; a non-ASCII digit string is not an id
            with self.assertRaises(ValueError, msg=bad):
                api_client._check_template_id(bad)
        self.assertEqual(api_client._group_id("12", "x"), 12)

    def test_two_damaged_files_in_one_second_keep_both(self):
        import state_io
        d = tempfile.mkdtemp(prefix="gym-r42-")
        path = os.path.join(d, "readiness.json")
        for body in ("A", "B", "C"):
            with open(path, "w") as f:
                f.write(body)
            with mock.patch("state_io.time.time", return_value=1_792_000_000):
                state_io.quarantine(path)
        kept = sorted(open(os.path.join(d, n)).read() for n in os.listdir(d) if ".corrupt-" in n)
        self.assertEqual(kept, ["A", "B", "C"])

    def test_a_pain_report_logged_while_apply_runs_still_blocks_the_save(self):
        d = tempfile.mkdtemp(prefix="gym-r42-")
        client = mock.Mock(data_dir=d, credentials={"token": "x", "unit": 1})
        client.get_library.return_value = []
        client.save_workout.return_value = {"code": 0, "data": {"id": 1}}
        program = {"goal_label": "G", "days": [{"name": "D", "exercises": [
            {"name": "Cable chest press", "primary": "chest", "sets": 2, "reps": 8, "load": 0, "rest_sec": 60}]}]}

        def prepare(nm, exercises):  # the vendor checks take a while; the pain report arrives meanwhile
            features.log_feedback({"pain": True, "exercise": "Cable chest press"}, d)
        client.prepare_workout.side_effect = prepare
        resolved = {"groupId": 7, "matched_name": "Cable Chest Press", "muscle": "chest", "confidence": "high"}
        with mock.patch.object(features, "resolve_exercise", return_value=resolved):
            out = features.apply_program(client, program, 0, None, dry_run=False)
        client.save_workout.assert_not_called()
        self.assertEqual((out["created"], out["failed"]), (0, 1))
        self.assertEqual(out["results"][0]["error"], "AvoidListChanged")

    def test_an_accessory_without_an_id_does_not_crash_the_pages(self):
        client = mock.Mock()
        client.credentials = {"token": "x"}
        client.get_accessories.return_value = [{"name": "noid"}, {"id": 1, "name": "Bar"}, "junk"]
        client.get_exercise_detail.return_value = {"id": 5, "title": "T", "accessories": "1", "actionLibraryList": [{"id": 1}]}
        client.get_library.return_value = []
        with mock.patch.object(gym_app, "client", client):
            resp = gym_app.app.test_client().get("/exercise/5", headers=BASIC)
        self.assertLess(resp.status_code, 500)

    def test_the_library_page_survives_an_accessory_without_an_id(self):
        client = mock.Mock()
        client.credentials = {"token": "x"}
        client.get_accessories.return_value = [{"name": "noid"}, {"id": 1, "name": "Bar"}, "junk"]
        client.get_library.return_value = [{"id": 5, "title": "T", "mainMuscleGroupName": "chest", "accessories": "1"}]
        client.get_categories.return_value = [{"id": 1, "name": "Chest"}]
        with mock.patch.object(gym_app, "client", client):
            resp = gym_app.app.test_client().get("/library", headers=BASIC)
        self.assertEqual(resp.status_code, 200)

    def test_the_pain_recheck_also_matches_the_coachs_own_name(self):
        d = tempfile.mkdtemp(prefix="gym-r42-")
        client = mock.Mock(data_dir=d, credentials={"token": "x", "unit": 1})
        client.get_library.return_value = []
        client.save_workout.return_value = {"code": 0, "data": {"id": 1}}
        program = {"goal_label": "G", "days": [{"name": "D", "exercises": [
            {"name": "Cable chest press", "primary": "chest", "sets": 2, "reps": 8, "load": 0, "rest_sec": 60}]}]}
        client.prepare_workout.side_effect = lambda nm, ex: features.log_feedback({"pain": True, "exercise": "Cable chest press"}, d)
        resolved = {"groupId": 7, "matched_name": "Pec Machine", "muscle": "chest", "confidence": "high"}  # the library's title differs from the coach's name
        with mock.patch.object(features, "resolve_exercise", return_value=resolved):
            out = features.apply_program(client, program, 0, None, dry_run=False)
        client.save_workout.assert_not_called()
        self.assertEqual(out["results"][0]["error"], "AvoidListChanged")


class Round43(unittest.TestCase):
    def test_a_pain_report_waits_for_a_save_in_flight_and_the_save_sees_the_earlier_ones(self):
        import threading, time as _time
        d = tempfile.mkdtemp(prefix="gym-r43-")
        client = mock.Mock(data_dir=d, credentials={"token": "x", "unit": 1})
        client.get_library.return_value = []
        program = {"goal_label": "G", "days": [{"name": "D", "exercises": [
            {"name": "Cable chest press", "primary": "chest", "sets": 2, "reps": 8, "load": 0, "rest_sec": 60}]}]}
        seen = {}

        def save(nm, exercises):
            late = threading.Thread(target=lambda: features.log_feedback({"pain": True, "exercise": "Cable chest press"}, d))
            late.start()
            _time.sleep(0.3)
            seen["still_waiting"] = late.is_alive()  # the report cannot land between the last check and the vendor's answer
            seen["thread"] = late
            return {"code": 0, "data": {"id": 1}}
        client.save_workout.side_effect = save
        resolved = {"groupId": 7, "matched_name": "Cable Chest Press", "muscle": "chest", "confidence": "high"}
        with mock.patch.object(features, "resolve_exercise", return_value=resolved):
            features.apply_program(client, program, 0, None, dry_run=False)
        seen["thread"].join(5)
        self.assertTrue(seen["still_waiting"])
        self.assertIn("Cable chest press", features._feedback_profile(d)["avoid"])  # recorded right after the save

    def test_an_iso_timestamp_with_a_zone_is_the_local_day(self):
        from datetime import date
        import time as _time
        old = os.environ.get("TZ")
        os.environ["TZ"] = "America/Los_Angeles"; _time.tzset()
        try:
            self.assertEqual(features._parse_date("2026-10-28T01:00:00Z"), date(2026, 10, 27))
            self.assertEqual(features._parse_date("2026-10-27T01:00:00Z"), date(2026, 10, 26))
            self.assertEqual(features._parse_date("2026-10-27T09:00:00-07:00"), date(2026, 10, 27))
            self.assertEqual(features._parse_date("2026-10-27 09:00:00"), date(2026, 10, 27))  # no zone: the date as written
            self.assertEqual(features._parse_date("2026-10-27"), date(2026, 10, 27))
        finally:
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            _time.tzset()

    def test_a_two_level_set_has_no_load_nobody_carried(self):
        from harness.normalize import set_load
        row = lambda w: {"finishedCount": len(w), "capacity": sum(w), "trainingInfoDetail": {"weights": w}}
        self.assertEqual(set_load(row([100.0] * 5 + [75.0] * 5)), 75.0)
        self.assertEqual(set_load(row([100.0] * 5 + [45.0] * 5)), 45.0)
        self.assertEqual(set_load(row([100.0] * 5 + [40.0] * 5)), 40.0)
        self.assertEqual(set_load(row([100.0] * 10)), 100.0)  # the controls
        self.assertEqual(set_load(row([40.0, 40.0, 40.0, 20.0, 60.0])), 40.0)
        self.assertEqual(set_load(row([100.0] * 9 + [85.0])), 100.0)

    def test_a_flag_that_is_not_true_never_becomes_confirmed_by_an_unrelated_save(self):
        for flag in ("false", 1, "no", "0", 0, None, []):
            with mock.patch.dict(os.environ, {"DATA_DIR": tempfile.mkdtemp(prefix="gym-r43-")}):
                client = api_client.SpeedianceClient()
            client.save_config("42", "tok", "Global", 1, "", 1, False, [], [], unit_confirmed=True)
            client.credentials["unit_confirmed"] = flag
            client.save_config("42", "tok", "Global", 1, "other", 1, False, [], [])
            self.assertFalse(api_client.unit_is_confirmed(client.credentials), repr(flag))
        client.credentials["unit_confirmed"] = True
        client.save_config("42", "tok", "Global", 1, "other", 1, False, [], [])
        self.assertTrue(api_client.unit_is_confirmed(client.credentials))

    def test_a_malformed_opt_out_does_not_switch_the_stored_safety_lists_off(self):
        for value in (None, 0, "", [], "false"):
            with mock.patch.object(features, "_feedback_profile", return_value={"avoid": ["Cable chest press"]}) as spy, \
                    mock.patch.object(features, "_authed", return_value=True):
                gym_app.app.test_client().post("/api/coach/program", headers=BASIC, json={"use_stored_feedback": value})
            self.assertTrue(spy.called, repr(value))
        with mock.patch.object(features, "_feedback_profile", return_value={"avoid": []}) as spy:
            gym_app.app.test_client().post("/api/coach/program", headers=BASIC, json={"use_stored_feedback": False})
        self.assertFalse(spy.called)

    def test_a_boolean_load_is_not_a_number(self):
        with self.assertRaises(ValueError):
            features._program_loads({"exercises": [{"name": "x", "load": True}]})
        self.assertEqual(features._program_loads({"exercises": [{"name": "x", "load": "12"}, {"name": "y"}]}), [12.0, 0.0])

    def test_a_caller_supplied_feedback_still_keeps_the_stored_avoid_list_unless_turned_off_with_false(self):
        stored = {"avoid": ["Cable chest press"], "events": 1}
        for value, kept in ((None, True), (0, True), ("false", True), (False, False)):
            with mock.patch.object(features, "_feedback_profile", return_value=stored) as spy, mock.patch.object(features, "_authed", return_value=True):
                gym_app.app.test_client().post("/api/coach/program", headers=BASIC, json={"feedback": {"avoid": ["Squat"]}, "use_stored_feedback": value})
            self.assertEqual(spy.called, kept, repr(value))

    def test_a_three_level_set_anchors_on_what_three_quarters_of_the_reps_carried(self):
        from harness.normalize import set_load
        w = [100.0] * 4 + [75.0] * 2 + [50.0] * 4
        self.assertEqual(set_load({"finishedCount": 10, "capacity": sum(w), "trainingInfoDetail": {"weights": w}}), 50.0)


class Round44(unittest.TestCase):
    def test_a_feedback_file_that_becomes_unreadable_during_apply_stops_the_save(self):
        d = tempfile.mkdtemp(prefix="gym-r44-")
        client = mock.Mock(data_dir=d, credentials={"token": "x", "unit": 1})
        client.get_library.return_value = []
        client.save_workout.return_value = {"code": 0, "data": {"id": 1}}
        program = {"goal_label": "G", "days": [{"name": "D", "exercises": [
            {"name": "Cable chest press", "primary": "chest", "sets": 2, "reps": 8, "load": 0, "rest_sec": 60}]}]}
        path = features._feedback_path(d)

        def prepare(nm, exercises):
            features.log_feedback({"pain": True, "exercise": "Cable chest press"}, d)
            os.chmod(path, 0)
        client.prepare_workout.side_effect = prepare
        resolved = {"groupId": 7, "matched_name": "Cable Chest Press", "muscle": "chest", "confidence": "high"}
        try:
            with mock.patch.object(features, "resolve_exercise", return_value=resolved):
                out = features.apply_program(client, program, 0, None, dry_run=False)
            if os.access(path, os.R_OK):
                self.skipTest("running as a user that ignores file modes")
        finally:
            if os.path.exists(path):
                os.chmod(path, 0o600)
        client.save_workout.assert_not_called()
        self.assertEqual(out["results"][0]["error"], "FeedbackUnreadable")

    def test_three_letter_plurals_ups_match_the_singular_title(self):
        import coach
        for reported, title in (("Push-ups", "Push-Up"), ("pull ups", "Pull-Up"), ("Chin-ups", "Chin-Up"), ("Sit-ups", "Sit-Up"), ("Step ups", "Step-Up")):
            self.assertTrue(coach._is_avoided(title, {coach._norm_name(reported)}), reported)
        self.assertEqual(coach._stem("abs"), "abs")
        self.assertEqual(coach._stem("lats"), "lat")

    def test_one_shared_generic_word_is_not_a_match_when_no_muscle_is_known(self):
        lib = [{"id": 1, "title": "Triceps Extension", "mainMuscleGroupName": "triceps", "isLeftRight": 0},
               {"id": 2, "title": "Hip Abduction", "mainMuscleGroupName": "glutes", "isLeftRight": 0},
               {"id": 3, "title": "Leg Press", "mainMuscleGroupName": "quadriceps", "isLeftRight": 0},
               {"id": 4, "title": "Plank Hold", "mainMuscleGroupName": "abs", "isLeftRight": 0}]
        for name in ("Back Extension", "Hip Adduction", "Kettlebell Swing Press"):
            got = features.resolve_exercise(lib, name, None)
            self.assertTrue(got is None or got["confidence"] == "low", name)
        self.assertNotEqual(features.resolve_exercise(lib, "Hip Abduction", None)["confidence"], "low")  # full coverage still resolves
        self.assertNotEqual(features.resolve_exercise(lib, "Plank Hold", None)["confidence"], "low")

    def test_trunk_is_not_a_run_and_a_strict_unit_flag_is_a_real_true(self):
        import coach
        self.assertNotEqual(coach.classify_exercise("Cable Trunk Rotation")[1], "cardio")
        self.assertEqual(coach.classify_exercise("Treadmill Run")[1], "cardio")
        for flag in ("false", "no", 1):
            with mock.patch.dict(os.environ, {"DATA_DIR": tempfile.mkdtemp(prefix="gym-r44-")}):
                client = api_client.SpeedianceClient()
            client.save_config("42", "tok", "Global", 1, "", 1, False, [], [], unit_confirmed=flag)
            self.assertFalse(api_client.unit_is_confirmed(client.credentials), repr(flag))


class Round45(unittest.TestCase):
    def test_a_fractional_rep_count_is_not_a_set(self):
        from harness.normalize import session_with_sets, set_load
        row = {"finishedCount": 0.1, "targetCount": 10, "capacity": 740, "trainingInfoDetail": {}}
        self.assertIsNone(set_load(row))
        record = {"trainingId": 1, "startTimestamp": 1_792_000_000, "title": "T"}
        detail = [{"actionLibraryName": "Cable Row", "finishedReps": [row, {**row, "finishedCount": 10, "capacity": 740}]}]
        sets = session_with_sets(record, detail).sets
        self.assertEqual([(s.reps, s.load) for s in sets], [(None, None), (10, 74.0)])
        for bad in (2.5, 9.99):
            self.assertIsNone(set_load({**row, "finishedCount": bad}), bad)
        self.assertEqual(set_load({**row, "finishedCount": 10.0}), 74.0)  # a whole number sent as 10.0 is fine

    def test_running_is_cardio_and_a_unicode_date_is_not_a_date(self):
        from datetime import date
        import coach
        for name in ("Running", "Treadmill Run", "Runs", "run"):
            self.assertEqual(coach.classify_exercise(name)[1], "cardio", name)
        self.assertNotEqual(coach.classify_exercise("Cable Trunk Rotation")[1], "cardio")
        self.assertIsNone(features._parse_date("２０２６-１０-２８"))
        self.assertEqual(features._parse_date("2026-10-28"), date(2026, 10, 28))

    def test_the_library_page_survives_an_accessory_with_a_non_string_name(self):
        client = mock.Mock()
        client.credentials = {"token": "x"}
        client.get_accessories.return_value = [{"id": 1, "name": None}, {"id": 2, "name": 5}, {"id": 3, "name": ["x"]}, {"id": 4, "name": "Bar"}]
        client.get_library.return_value = [{"id": 5, "title": "T", "mainMuscleGroupName": "chest", "accessories": "1,2,3,4"}]
        client.get_categories.return_value = [{"id": 1, "name": "Chest"}]
        with mock.patch.object(gym_app, "client", client):
            resp = gym_app.app.test_client().get("/library", headers=BASIC)
        self.assertEqual(resp.status_code, 200)

    def test_a_fractional_target_is_not_a_target(self):
        from harness.normalize import session_with_sets
        record = {"trainingId": 1, "startTimestamp": 1_792_000_000, "title": "T"}
        detail = [{"actionLibraryName": "Cable Row", "finishedReps": [{"finishedCount": 8, "targetCount": 2.5, "capacity": 640, "trainingInfoDetail": {}}]}]
        self.assertIsNone(session_with_sets(record, detail).sets[0].target)


class Round46(unittest.TestCase):
    def test_opposite_directions_are_different_exercises(self):
        pairs = (("Shoulder External Rotation", "Shoulder Internal Rotation"), ("Hip Adduction", "Hip Abduction"),
                 ("Forward Lunge", "Backward Lunge"), ("Inner Thigh Squeeze", "Outer Thigh Squeeze"), ("Wrist Flexion Curl", "Wrist Extension Curl"))
        for wanted, offered in pairs:
            lib = [{"id": 1, "title": offered, "mainMuscleGroupName": "shoulders", "isLeftRight": 1}]
            got = features.resolve_exercise(lib, wanted, "shoulders", True)
            self.assertTrue(got is None or got["confidence"] == "low", (wanted, offered))
            lib[0]["title"] = wanted
            self.assertNotEqual(features.resolve_exercise(lib, wanted, "shoulders", True)["confidence"], "low", wanted)  # its own name still resolves
