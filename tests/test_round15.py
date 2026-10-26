"""Review round 15, batch A (safety): the account unit must be confirmed before loads are saved; a pain/avoid report
outlives everything else; a vendor outage is never read as an empty library or as "no feedback"; every day is checked
before the first write; redirects never carry the session token."""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

TOKEN = "t" * 32
os.environ["GYM_API_TOKEN"] = TOKEN
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="gym-test-data-")
os.environ.pop("GYM_AUTH_DISABLED", None)
os.environ.pop("GYM_ALLOW_WRITES", None)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import api_client  # noqa: E402
import features  # noqa: E402
import mcp_server  # noqa: E402
from harness import speediance  # noqa: E402

PRESS = {"groupId": 7, "matched_name": "Cable Chest Press", "muscle": "chest", "confidence": "high"}


def program(*days):
    return {"goal_label": "G", "days": list(days)}


def day(name="D", ex_name="Press", **ex):
    base = {"name": ex_name, "primary": "chest", "sets": 2, "reps": 8, "load": 0, "rest_sec": 90}
    base.update(ex)
    return {"name": name, "exercises": [base]}


def make_client(creds=None):
    client = mock.Mock()
    client.credentials = {"token": "x", "unit": 0} if creds is None else creds
    client.get_library.return_value = []
    client.save_workout.return_value = {"code": 0, "data": {"id": 1}}
    return client


class Base(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(features, "resolve_exercise", return_value=dict(PRESS))
        patcher.start()
        self.addCleanup(patcher.stop)


class TheEditorsExerciseLimitIsTheServersLimit(unittest.TestCase):
    def test_fifty_exercises_pass_and_fifty_one_do_not(self):
        ex = {"groupId": 7, "sets": [{"reps": 8, "weight": 10, "rest": 60}]}
        api_client.validate_workout("n", [dict(ex) for _ in range(50)])
        with self.assertRaises(ValueError):
            api_client.validate_workout("n", [dict(ex) for _ in range(51)])


class UnitMustBeConfirmedBeforeLoadsAreSaved(Base):
    def apply(self, creds, **ex):
        client = make_client(creds)
        out = features.apply_program(client, program(day(**ex)), 0, None, dry_run=False, load_unit="kg")
        return client, out

    def test_an_unconfirmed_metric_default_refuses_loads_and_writes_nothing(self):
        client = make_client({"token": "x", "unit": 0})
        with self.assertRaises(ValueError) as ctx:
            features.apply_program(client, program(day(load=40)), 0, None, dry_run=False, load_unit="kg")
        self.assertIn("/settings", str(ctx.exception))
        client.save_workout.assert_not_called()

    def test_a_preview_is_gated_too(self):
        with self.assertRaises(ValueError):
            features.apply_program(make_client({"token": "x", "unit": 0}), program(day(load=40)), 0, None, load_unit="kg")

    def test_confirmed_or_imperial_accounts_pass(self):
        for creds in ({"token": "x", "unit": 0, "unit_confirmed": True}, {"token": "x", "unit": 1}):
            client, out = self.apply(creds, load=40)
            self.assertEqual(out["created"], 1, creds)

    def test_a_program_without_loads_needs_no_confirmation(self):
        client = make_client({"token": "x", "unit": 0})
        out = features.apply_program(client, program(day(load=0)), 0, None, dry_run=False)
        self.assertEqual(out["created"], 1)

    def test_non_dict_credentials_never_count_as_confirmed(self):
        for creds in (None, "yes", ["unit_confirmed"]):
            client = make_client()
            client.credentials = creds
            with self.assertRaises(ValueError):
                features.apply_program(client, program(day(load=40)), 0, None, load_unit="kg")

    def test_the_flag_is_stored_preserved_and_set_by_update_unit(self):
        with mock.patch.dict(os.environ, {"DATA_DIR": tempfile.mkdtemp(prefix="gym-client-")}):
            client = api_client.SpeedianceClient()
        client.save_config("1", "tok", "Global", 0, "", 1, False, [], [])
        self.assertFalse(client.credentials["unit_confirmed"])
        client.save_config("1", "tok", "Global", 1, "", 1, False, [], [], unit_confirmed=True)
        self.assertTrue(client.credentials["unit_confirmed"])
        client.save_config("1", "tok2", "Global", 1, "", 1, False, [], [])  # a login must not forget the choice
        self.assertTrue(client.credentials["unit_confirmed"])
        client.save_config("1", "tok2", "Global", 0, "", 1, False, [], [], unit_confirmed=False)
        self.assertFalse(client.credentials["unit_confirmed"])


class TheAvoidListWinsOverTheLibraryMatch(Base):
    def test_a_library_match_the_user_reported_pain_on_is_not_saved(self):
        # the coach called it "Press"; the library resolved it to "Cable Chest Press", which is on the avoid list
        exercises, resolved, unresolved = features.program_day_to_payload([], day(), "kg", False, {"chest press"})
        self.assertEqual((exercises, resolved), ([], []))
        self.assertEqual(unresolved[0]["reason"], "on your avoid list")

    def test_the_coachs_own_name_is_checked_too(self):
        with mock.patch.object(features, "resolve_exercise", return_value=dict(PRESS, matched_name="Something Else")):
            exercises, _, unresolved = features.program_day_to_payload([], day(ex_name="Bench Press"), "kg", False, {"bench press"})
        self.assertEqual(exercises, [])
        self.assertEqual(len(unresolved), 1)

    def test_nothing_is_dropped_without_an_avoid_entry(self):
        exercises, resolved, unresolved = features.program_day_to_payload([], day(), "kg", False)
        self.assertEqual((len(exercises), len(resolved), unresolved), (1, 1, []))

    def test_apply_program_reads_the_stored_feedback_and_the_programs_own_list(self):
        data_dir = tempfile.mkdtemp(prefix="gym-fb-")
        features.log_feedback({"pain": True, "exercise": "Cable Chest Press"}, data_dir)
        client = make_client()
        client.data_dir = data_dir
        out = features.apply_program(client, program(day()), 0, None, dry_run=True)
        self.assertEqual(out["results"][0]["unresolved"][0]["reason"], "on your avoid list")
        client.data_dir = tempfile.mkdtemp(prefix="gym-fb-")  # nothing stored: only the program's own list applies
        prog = program(day())
        prog["feedback_applied"] = {"avoid": ["Chest Press"]}
        out = features.apply_program(client, prog, 0, None, dry_run=True)
        self.assertEqual(out["results"][0]["unresolved"][0]["reason"], "on your avoid list")


class ProgramShapesAreBounded(Base):
    def test_one_sided_exercises_become_left_right_pairs(self):
        exercises, _, _ = features.program_day_to_payload([], day(sets=3, unilateral=True), "kg", False)
        self.assertEqual(len(exercises[0]["sets"]), 6)
        self.assertEqual(len(features.program_day_to_payload([], day(sets=3), "kg", False)[0][0]["sets"]), 3)

    def test_a_pair_expansion_cannot_exceed_the_set_cap(self):
        with self.assertRaises(ValueError):
            features.program_day_to_payload([], day(sets=api_client.MAX_SETS // 2 + 1, unilateral=True), "kg", False)
        features.program_day_to_payload([], day(sets=api_client.MAX_SETS // 2, unilateral=True), "kg", False)

    def test_a_zero_second_rest_is_a_real_rest_and_the_default_only_fills_none(self):
        self.assertEqual(features.program_day_to_payload([], day(rest_sec=0), "kg", False)[0][0]["sets"][0]["rest"], 0)
        self.assertEqual(features.program_day_to_payload([], day(rest_sec=None), "kg", False)[0][0]["sets"][0]["rest"], 90)

    def test_a_rest_beyond_the_machine_limit_is_refused_before_anything_is_built(self):
        for rest in (api_client.MAX_REST_S + 1, -1, 10**9, "x"):
            with self.assertRaises(ValueError, msg=repr(rest)):
                features.program_day_to_payload([], day(rest_sec=rest), "kg", False)

    def test_coach_modes_that_are_not_applied_are_called_out(self):
        client = make_client({"token": "x", "unit": 1})
        out = features.apply_program(client, program(day(mode="eccentric")), 0, None, dry_run=True, load_unit="lb")
        self.assertIn("Standard", out["results"][0]["notes"][0])
        self.assertEqual(out["results"][0]["resolved"][0]["coach_mode"], "eccentric")
        out = features.apply_program(client, program(day()), 0, None, dry_run=True, load_unit="lb")
        self.assertNotIn("notes", out["results"][0])

    def test_a_program_has_a_bounded_number_of_days(self):
        client = make_client()
        features.apply_program(client, program(*[day(f"D{i}") for i in range(features.MAX_PROGRAM_DAYS)]), None, None)
        with self.assertRaises(ValueError):
            features.apply_program(client, program(*[day(f"D{i}") for i in range(features.MAX_PROGRAM_DAYS + 1)]), None, None)

    def test_the_library_is_read_strictly_so_a_hole_is_an_error(self):
        client = make_client()
        features.apply_program(client, program(day()), 0, None)
        client.get_library.assert_called_with(strict=True)


class EveryDayIsCheckedBeforeTheFirstWrite(Base):
    def test_a_day_that_fails_the_library_aware_check_stops_every_write(self):
        client = make_client({"token": "x", "unit": 1})

        def prepare(name, exercises):
            if name.endswith("Bad"):
                raise ValueError("exercise 1: one-cable lift over 110 lb")

        client.prepare_workout.side_effect = prepare
        with self.assertRaises(ValueError):
            features.apply_program(client, program(day("Good"), day("Bad")), None, None, dry_run=False)
        client.save_workout.assert_not_called()
        self.assertEqual(client.prepare_workout.call_count, 2)

    def test_a_write_time_validation_message_is_reported_for_that_day(self):
        client = make_client()
        client.save_workout.side_effect = ValueError("exercise 1, set 2: weight is out of range")
        out = features.apply_program(client, program(day()), 0, None, dry_run=False)
        self.assertEqual((out["failed"], out["results"][0]["error"]), (1, "ValueError"))
        self.assertIn("weight is out of range", out["results"][0]["message"])

    def test_a_vendor_outage_message_is_not_echoed(self):
        client = make_client()
        client.save_workout.side_effect = api_client.UpstreamError("down https://secret?token=1")
        out = features.apply_program(client, program(day()), 0, None, dry_run=False)
        self.assertNotIn("message", out["results"][0])
        self.assertNotIn("secret", json.dumps(out))


class PainAndAvoidReportsOutliveEverythingElse(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="gym-fb-")

    def test_a_pain_report_survives_far_more_than_100_later_entries(self):
        features.log_feedback({"pain": True, "exercise": "Lat Pulldown"}, self.dir)
        for i in range(150):
            features.log_feedback({"rpe": 7, "difficulty": "ok"}, self.dir)
        profile = features._feedback_profile(self.dir, days=3650)
        self.assertEqual(profile["avoid"], ["Lat Pulldown"])
        stored = json.load(open(features._feedback_path(self.dir)))
        self.assertEqual(len([e for e in stored if not e.get("pain")]), 100)  # ordinary entries still age out

    def test_the_standing_list_is_itself_bounded(self):
        for i in range(520):
            features.log_feedback({"avoid": f"Move {i}"}, self.dir)
        stored = json.load(open(features._feedback_path(self.dir)))
        self.assertEqual(len(stored), 500)
        self.assertEqual(stored[-1]["avoid"], "Move 519")  # the newest are the ones kept

    def test_the_file_stays_in_time_order(self):
        for payload in ({"pain": True, "exercise": "A"}, {"rpe": 6}, {"avoid": "B"}):
            features.log_feedback(payload, self.dir)
        stamps = [e["ts"] for e in json.load(open(features._feedback_path(self.dir)))]
        self.assertEqual(stamps, sorted(stamps))

    def test_unreadable_feedback_is_reported_not_treated_as_none(self):
        self.assertIsNone(features._feedback_read_error(self.dir))  # absent is fine
        features.log_feedback({"pain": True, "exercise": "Row"}, self.dir)
        self.assertIsNone(features._feedback_read_error(self.dir))
        with open(features._feedback_path(self.dir), "w") as f:
            f.write("{not json")
        self.assertEqual(features._feedback_read_error(self.dir), "JSONDecodeError")
        self.assertEqual(features._feedback_profile(self.dir)["unreadable"], "JSONDecodeError")
        with open(features._feedback_path(self.dir), "w") as f:
            f.write('{"a": 1}')
        self.assertEqual(features._feedback_read_error(self.dir), "ValueError")

    def test_the_route_says_when_the_avoid_list_could_not_be_read(self):
        import app as gym_app
        import base64
        basic = {"Authorization": "Basic " + base64.b64encode(f"gym:{TOKEN}".encode()).decode()}
        path = features._feedback_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("{not json")
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        resp = gym_app.app.test_client().post("/api/coach/program", headers=basic,
                                              json={"goal": "muscle", "use_stored_readiness": False})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["feedback_error"], "JSONDecodeError")
        os.remove(path)
        resp = gym_app.app.test_client().post("/api/coach/program", headers=basic,
                                              json={"goal": "muscle", "use_stored_readiness": False})
        self.assertNotIn("feedback_error", resp.get_json())

    def test_the_mcp_program_says_when_the_avoid_list_could_not_be_read(self):
        with open(features._feedback_path(self.dir), "w") as f:
            f.write("{not json")
        client = mock.Mock(data_dir=self.dir)
        tool = getattr(mcp_server.generate_program, "fn", mcp_server.generate_program)
        with mock.patch.object(mcp_server, "_client", return_value=client), mock.patch.object(mcp_server, "_require_auth"):
            out = tool(goal="muscle", days_per_week=3, experience="intermediate")
        self.assertEqual(out["feedback_error"], "JSONDecodeError")


class TheMcpSearchReadsTheLibraryStrictly(unittest.TestCase):
    def test_a_partial_library_is_an_error_not_a_short_result_list(self):
        client = mock.Mock()
        client.get_library.side_effect = api_client.UpstreamError("the exercise library could not be loaded completely")
        tool = getattr(mcp_server.search_exercises, "fn", mcp_server.search_exercises)
        with mock.patch.object(mcp_server, "_client", return_value=client), mock.patch.object(mcp_server, "_require_auth"):
            out = tool("press")
        client.get_library.assert_called_with(strict=True)
        self.assertIsInstance(out, dict)
        self.assertIn("error", out)


class DatesComeFromWhenTheSessionHappened(unittest.TestCase):
    def test_a_start_time_beats_the_record_creation_time(self):
        rec = {"createTime": "2026-10-25 08:00:00", "startTimestamp": "2026-10-20 18:00:00"}
        self.assertEqual(str(features._extract_date(rec)), "2026-10-20")

    def test_unlisted_date_like_keys_still_work(self):
        self.assertEqual(str(features._extract_date({"scheduleDate": "2026-10-21"})), "2026-10-21")

    def test_the_days_date_beats_a_plans_creation_time(self):
        out = []
        node = {"date": "2026-10-22", "trainingPlanList": [{"title": "Push", "createTime": "2026-10-01 09:00:00"}]}
        features._walk_events(node, out)
        self.assertEqual([(str(d), name) for d, name in out], [("2026-10-22", "Push")])


class AnIncompleteLibraryIsNeverCachedOrPassedOff(unittest.TestCase):
    def client(self):
        with mock.patch.dict(os.environ, {"DATA_DIR": tempfile.mkdtemp(prefix="gym-lib-")}):
            client = api_client.SpeedianceClient()
        client.save_config("1", "tok", "Global", 0, "", 1, False, [], [])
        client.library_cache = None
        client.get_categories = lambda strict=False: [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]
        return client

    @staticmethod
    def resp(body, status=200):
        r = mock.Mock(status_code=status, text="")
        r.json.return_value = body
        return r

    def test_a_failed_category_is_not_cached_and_raises_under_strict(self):
        client = self.client()
        ok = self.resp({"data": [{"actionLibraryGroupList": [{"id": 5, "name": "Press"}]}]})
        client._request = lambda method, url, **kw: ok if "tabId=1" in url else self.resp({}, 503)
        client.get_batch_details = lambda ids, strict=False: [{"id": i, "name": "Press"} for i in ids]
        with self.assertRaises(api_client.UpstreamError):
            client.get_library(strict=True)
        self.assertFalse(client.library_cache)
        partial = client.get_library()  # the lenient web page still renders what it has
        self.assertEqual([e["id"] for e in partial], [5])
        self.assertFalse(client.library_cache)  # but never remembers a library with a hole in it

    def test_a_complete_library_is_cached(self):
        client = self.client()
        ok = self.resp({"data": [{"actionLibraryGroupList": [{"id": 5, "name": "Press"}]}]})
        client._request = lambda method, url, **kw: ok
        client.get_batch_details = lambda ids, strict=False: [{"id": i, "name": "Press"} for i in ids]
        self.assertEqual([e["id"] for e in client.get_library(strict=True)], [5])
        self.assertTrue(client.library_cache)


class TheSessionTokenNeverFollowsARedirect(unittest.TestCase):
    def test_a_redirect_is_returned_as_the_answer_and_the_target_is_never_contacted(self):
        hits = []

        class Target(BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(dict(self.headers))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *a):
                pass

        target = HTTPServer(("127.0.0.1", 0), Target)
        port = target.server_address[1]

        class Redirector(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{port}/steal")
                self.end_headers()

            def log_message(self, *a):
                pass

        redirector = HTTPServer(("127.0.0.1", 0), Redirector)
        for server in (target, redirector):
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        try:
            status, _ = speediance.default_transport(
                "GET", f"http://127.0.0.1:{redirector.server_address[1]}/x", {"Token": "secret"}, None)
        except (OSError, urllib.error.URLError):
            status = None
        self.assertEqual(hits, [])  # the token never reached the redirect target
        self.assertNotEqual(status, 200)

    def test_the_redirect_handler_refuses_every_redirect(self):
        handler = speediance._NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "Found", {}, "http://elsewhere.invalid/"))


class RequestBodiesAreAlwaysObjects(unittest.TestCase):
    ROUTES = ("/api/coach/readiness", "/api/coach/autoregulate", "/api/coach/program", "/api/coach/feedback",
              "/api/coach/critique", "/api/coach/apply", "/api/schedule", "/api/schedule_course")

    def post(self, path, body, client=None):
        import app as gym_app
        import base64
        basic = {"Authorization": "Basic " + base64.b64encode(f"gym:{TOKEN}".encode()).decode()}
        fake = client or make_client({"token": "x", "unit": 1, "unit_confirmed": True})
        fake.data_dir = tempfile.mkdtemp(prefix="gym-body-")
        with mock.patch.object(gym_app, "client", fake), mock.patch.object(features, "_client", fake), \
                mock.patch.object(features, "_authed", return_value=True), \
                mock.patch.dict(os.environ, {"GYM_ALLOW_WRITES": "1"}):
            return gym_app.app.test_client().post(path, headers=basic, json=body)

    def test_a_list_string_or_number_body_is_a_client_error_never_a_500(self):
        for path in self.ROUTES:
            for body in ([1, 2], "text", 7, [], None):
                resp = self.post(path, body)
                self.assertLess(resp.status_code, 500, (path, body, resp.status_code))

    def test_schedule_validates_before_anything_is_forwarded(self):
        client = make_client({"token": "x", "unit": 1})
        client.schedule_workout.return_value = True
        client.schedule_course.return_value = True
        good = {"date": "2026-10-27", "templateCode": "abc123", "status": 1}
        self.assertEqual(self.post("/api/schedule", good, client).status_code, 200)
        for patch_ in ({"date": "tomorrow"}, {"date": "2026-13-40"}, {"date": "2026-10-27&x=1"}, {"status": 2}, {"status": True},
                       {"status": "1"}, {"status": -1}, {"templateCode": "a/../b"}, {"templateCode": "a,b"}, {"templateCode": "a b"}):
            client.schedule_workout.reset_mock()
            resp = self.post("/api/schedule", {**good, **patch_}, client)
            self.assertEqual(resp.status_code, 400, patch_)
            client.schedule_workout.assert_not_called()
            self.assertNotIn("x=1", resp.get_data(as_text=True))
        goodc = {"date": "2026-10-27", "courseId": 55, "status": 1}
        self.assertEqual(self.post("/api/schedule_course", goodc, client).status_code, 200)
        for patch_ in ({"date": "nope"}, {"status": 9}, {"status": False}, {"courseId": "5/../6"}):
            client.schedule_course.reset_mock()
            self.assertEqual(self.post("/api/schedule_course", {**goodc, **patch_}, client).status_code, 400, patch_)
            client.schedule_course.assert_not_called()

    def test_removing_a_schedule_with_status_zero_still_works(self):
        client = make_client({"token": "x", "unit": 1})
        client.schedule_workout.return_value = True
        resp = self.post("/api/schedule", {"date": "2026-10-27", "templateCode": "abc", "status": 0}, client)
        self.assertEqual(resp.status_code, 200)
        client.schedule_workout.assert_called_once_with("2026-10-27", "abc", 0)


if __name__ == "__main__":
    unittest.main()
