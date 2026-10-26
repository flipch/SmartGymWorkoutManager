"""Review round 14: the account-unit contract, the save path never drops or fakes anything, the calendar in its real
shape, the MCP closure class, fuzzy pain exclusions, visible readiness gaps, and small hardening."""
import ast
import base64
import contextlib
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
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
import mcp_server  # noqa: E402
from harness import planner, tracer, whoop_auth, whoop_client as wc  # noqa: E402
from harness.model import Plan, Reading  # noqa: E402

BASIC = {"Authorization": "Basic " + base64.b64encode(f"gym:{TOKEN}".encode()).decode()}
NODE = shutil.which("node")
ROOT = os.path.join(os.path.dirname(__file__), "..")


class Resp:
    def __init__(self, status=200, body=None, text=""):
        self.status_code, self._body, self.text = status, body, text
        self.request = mock.Mock(headers={})

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


def make_client(unit=0):
    with mock.patch.dict(os.environ, {"DATA_DIR": tempfile.mkdtemp(prefix="gym-client-")}):
        client = api_client.SpeedianceClient()
    client.save_config("42", "vendor-token", "Global", unit, "", 1, False, [], [], unit_confirmed=True)
    return client


def coach_names():
    return [e["name"].lower() for e in coach.CATALOG]


def ex(weight, group=7, **kw):
    return {"groupId": group, "sets": [{"reps": 10, "weight": weight, "rest": 60, "unit": "reps"}], **kw}


def saving_client(unit, unilateral=False, calls=None):
    """A client whose library lookups succeed and whose POST is captured."""
    client = make_client(unit)
    client.get_batch_details = lambda ids, **kw: [{"id": i, "actionLibraryList": [{"id": i + 1000}]} for i in ids]
    client.is_exercise_unilateral = lambda gid, **kw: unilateral

    def request(method, url, **kw):
        if calls is not None:
            calls.append((method, url, kw))
        return Resp(200, {"code": 0, "data": {"id": 1}})

    client._request = request
    return client


def posted_weights(calls):
    post = [c for c in calls if c[0] == "POST"][-1]
    return post[2]["json"]["actionLibraryList"][0]["weights"]


# ============================================================================== the account-unit contract
class TheTemplateApiStoresTheAccountsUnit(unittest.TestCase):
    """tests/test_unit.py (verified against the live machine): no conversion in Python; the editor's limits are 100 kg or
    220 lb (50 kg / 110 lb on one cable)."""

    def test_imperial_loads_are_stored_as_entered_and_capped_in_pounds(self):
        calls = []
        client = saving_client(1, calls=calls)
        client.save_workout("n", [ex(150)])
        self.assertEqual(posted_weights(calls), "150.0")
        client.save_workout("n", [ex(220)])
        with self.assertRaises(ValueError):
            client.save_workout("n", [ex(220.5)])

    def test_metric_loads_are_capped_in_kilograms(self):
        client = saving_client(0)
        client.save_workout("n", [ex(100)])
        with self.assertRaises(ValueError):
            client.save_workout("n", [ex(100.5)])
        with self.assertRaises(ValueError):
            client.save_workout("n", [ex(150)])  # a pound figure in a kg account

    def test_one_cable_caps_follow_the_unit(self):
        saving_client(1, unilateral=True).save_workout("n", [ex(110)])
        for unit, over in ((1, 110.5), (0, 50.5)):
            with self.assertRaises(ValueError) as ctx:
                saving_client(unit, unilateral=True).save_workout("n", [ex(over)])
            self.assertIn("single-cable", str(ctx.exception))
            self.assertIn("lb" if unit else "kg", str(ctx.exception))

    def test_validate_workout_defaults_to_metric(self):
        api_client.validate_workout("n", [ex(100)])
        with self.assertRaises(ValueError):
            api_client.validate_workout("n", [ex(101)])
        api_client.validate_workout("n", [ex(220)], imperial=True)


class CoachLoadsFollowTheAccount(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(features, "resolve_exercise",
                                    return_value={"groupId": 7, "matched_name": "Press", "muscle": "chest", "confidence": "high"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_apply(self, account_unit, load, load_unit):
        client = mock.Mock()
        client.credentials = {"token": "x", "unit": account_unit, "unit_confirmed": True}
        client.get_library.return_value = []
        client.save_workout.return_value = {"code": 0, "data": {"id": 1}}
        program = {"goal_label": "G", "days": [{"name": "D", "exercises": [
            {"name": "Press", "primary": "chest", "sets": 2, "reps": 8, "load": load, "rest_sec": 90}]}]}
        features.apply_program(client, program, 0, None, dry_run=False, load_unit=load_unit)
        return [s["weight"] for s in client.save_workout.call_args[0][1][0]["sets"]]

    def test_matching_units_are_not_converted(self):
        self.assertEqual(self.run_apply(1, 135, "lb"), [135, 135])  # an imperial account keeps pounds as pounds
        self.assertEqual(self.run_apply(0, 60, "kg"), [60, 60])

    def test_differing_units_are_converted_to_the_accounts(self):
        self.assertEqual(self.run_apply(0, 110, "lb"), [50.0, 50.0])
        self.assertEqual(self.run_apply(0, 135, "lb"), [61.5, 61.5])
        self.assertEqual(self.run_apply(1, 50, "kg"), [110, 110])

    def test_the_cap_is_in_the_accounts_unit(self):
        self.assertEqual(self.run_apply(1, 200, "lb"), [200, 200])
        with self.assertRaises(ValueError):
            self.run_apply(1, 230, "lb")
        with self.assertRaises(ValueError):
            self.run_apply(0, 110, "kg")


# ============================================================================== the save path
class SavingNeverDropsOrFakesAnything(unittest.TestCase):
    def test_an_exercise_missing_from_the_library_is_an_error_not_a_smaller_workout(self):
        calls = []
        client = saving_client(0, calls=calls)
        client.get_batch_details = lambda ids, **kw: [{"id": 7, "actionLibraryList": [{"id": 1007}]}]
        with self.assertRaises(ValueError) as ctx:
            client.save_workout("n", [ex(10, group=7), ex(10, group=8)], template_id=77)
        self.assertIn("not found in the exercise library", str(ctx.exception))
        self.assertEqual([c for c in calls if c[0] == "POST"], [])

    def test_a_library_outage_never_posts_an_empty_template_over_an_existing_one(self):
        calls = []
        client = make_client(0)
        client._request = mock.Mock(side_effect=lambda m, u, **kw: calls.append((m, u)) or Resp(500, {"message": "down"}))
        with self.assertRaises(api_client.UpstreamError):
            client.save_workout("x", [ex(10)], template_id=77)
        self.assertEqual([c for c in calls if c[0] == "POST"], [])

    @staticmethod
    def vendor(fail, calls):
        """A vendor where exactly one endpoint (matched by `fail`) is down and everything else works."""
        def request(method, url, **kw):
            calls.append((method, url))
            if fail in url:
                return Resp(503, {"message": "down"})
            if "/list?" in url:
                return Resp(200, {"code": 0, "data": [{"id": 7, "actionLibraryList": [{"id": 1007}]}]})
            if "actionLibraryGroup/7" in url:
                return Resp(200, {"code": 0, "data": {"id": 7, "isLeftRight": 1}})
            return Resp(200, {"code": 0, "data": {"id": 1}})
        return request

    def test_only_the_unilateral_lookup_down_is_an_error_not_bilateral(self):
        calls = []
        client = make_client(0)
        client._request = self.vendor("actionLibraryGroup/7", calls)
        with self.assertRaises(api_client.UpstreamError):  # not a silent "bilateral" that skips the 50 kg cap
            client.save_workout("x", [ex(80)])
        self.assertEqual([c for c in calls if c[0] == "POST"], [])

    def test_only_the_library_list_down_is_an_upstream_error_not_a_missing_exercise(self):
        calls = []
        client = make_client(0)
        client._request = self.vendor("/list?", calls)
        with self.assertRaises(api_client.UpstreamError):  # not the ValueError of "exercise not found"
            client.save_workout("x", [ex(10)], template_id=77)
        self.assertEqual([c for c in calls if c[0] == "POST"], [])

    def test_with_everything_up_the_same_vendor_saves(self):
        calls = []
        client = make_client(0)
        client._request = self.vendor("never-matches", calls)
        client.save_workout("x", [ex(40)])
        self.assertEqual(len([c for c in calls if c[0] == "POST"]), 1)

    def test_string_group_ids_are_the_same_exercise_and_get_the_same_caps(self):
        for gid in ("5", 5, " 5 "):
            with self.assertRaises(ValueError, msg=repr(gid)):
                saving_client(0, unilateral=True).save_workout("n", [ex(80, group=gid)])
        calls = []
        saving_client(0, unilateral=False, calls=calls).save_workout("n", [ex(80, group="5")])
        self.assertEqual(calls[-1][2]["json"]["actionLibraryList"][0]["groupId"], 5)

    def test_group_ids_cannot_inject_query_parameters_or_be_junk(self):
        for gid in ("1&ids=2", "1,2", -1, 0, 1.5, True, None, "", "abc", [1], 10**13):
            with self.assertRaises(ValueError, msg=repr(gid)):
                api_client.validate_workout("n", [ex(10, group=gid)])

    def test_the_library_query_uses_the_normalised_ids(self):
        urls = []
        client = make_client(0)
        def request(m, u, **kw):
            urls.append(u)
            if "/list?" in u:
                return Resp(200, {"code": 0, "data": [{"id": 5, "actionLibraryList": [{"id": 1005}]}]})
            if "actionLibraryGroup/5" in u:
                return Resp(200, {"code": 0, "data": {"id": 5, "isLeftRight": 0}})
            return Resp(200, {"code": 0, "data": {"id": 1}})

        client._request = request
        client.save_workout("n", [ex(10, group="5")])
        self.assertTrue(any(u.endswith("actionLibraryGroup/list?ids=5") for u in urls), urls)

    def test_a_non_200_save_is_an_error_even_with_a_success_looking_body(self):
        client = saving_client(0)
        for status in (201, 302, 500, 503):
            client._request = lambda m, u, status=status, **kw: Resp(status, {"code": 0, "data": {"id": 1}})
            with self.assertRaises(api_client.UpstreamError, msg=status):
                client.save_workout("n", [ex(10)])
        client._request = lambda m, u, **kw: Resp(200, None, "<html>")
        with self.assertRaises(api_client.UpstreamError):
            client.save_workout("n", [ex(10)])

    def test_zero_rest_is_kept(self):
        calls = []
        client = saving_client(0, calls=calls)
        client.save_workout("n", [{"groupId": 7, "sets": [{"reps": 5, "weight": 10, "rest": 0}]}])
        action = [c for c in calls if c[0] == "POST"][-1][2]["json"]["actionLibraryList"][0]
        self.assertEqual(action["breakTime"], "0")
        calls.clear()
        client.save_workout("n", [{"groupId": 7, "sets": [{"reps": 5, "weight": 10}]}])
        self.assertEqual([c for c in calls if c[0] == "POST"][-1][2]["json"]["actionLibraryList"][0]["breakTime"], "60")

    def test_strict_readers(self):
        client = make_client(0)
        client._request = lambda m, u, **kw: Resp(503, {"message": "down"})
        with self.assertRaises(api_client.UpstreamError):
            client.get_exercise_detail(7, strict=True)
        with self.assertRaises(api_client.UpstreamError):
            client.get_batch_details([7], strict=True)
        self.assertEqual(client.get_batch_details([7]), [])  # the library cache path keeps its tolerant behaviour


class RequestsHaveTimeoutsAndDoNotFollowRedirects(unittest.TestCase):
    def test_defaults_are_set_and_can_be_overridden(self):
        client = make_client(0)
        with mock.patch.object(api_client.requests, "request", return_value=Resp(200, {})) as req:
            client._request("GET", "https://api2.speediance.com/x")
            kw = req.call_args[1]
            self.assertEqual(kw["timeout"], api_client.REQUEST_TIMEOUT_S)
            self.assertLessEqual(api_client.REQUEST_TIMEOUT_S, 30)
            self.assertIs(kw["allow_redirects"], False)
            client._request("GET", "https://api2.speediance.com/x", timeout=3)
            self.assertEqual(req.call_args[1]["timeout"], 3)


class UpstreamReadersAreStrictAboutShape(unittest.TestCase):
    def test_a_tuple_of_kinds_has_an_explicit_empty_value(self):
        for body in ({"code": 0, "data": None}, {"code": 0}):
            self.assertEqual(api_client._json_data(Resp(200, body), "x", (dict, list), empty=dict), {})
        client = make_client(0)
        for body in ({"code": 0, "data": None}, {"code": 0}):
            client._request = lambda m, u, body=body, **kw: Resp(200, body)
            self.assertEqual(client.get_training_detail("1", "custom"), {})

    def test_require_ok(self):
        for resp in (Resp(302, {}), Resp(301, {}), Resp(200, None, "<html>login</html>")):
            with self.assertRaises(api_client.UpstreamError):
                api_client._require_ok(resp, "x")
        for resp in (Resp(204, None), Resp(200, None, ""), Resp(200, {"code": 0})):
            self.assertIsNone(api_client._require_ok(resp, "x"))

    def test_a_refused_unit_change_does_not_flip_the_local_unit(self):
        client = make_client(0)
        client._request = lambda m, u, **kw: Resp(200, {"code": 1001, "message": "no"})
        ok, msg = client.update_unit(1)
        self.assertFalse(ok)
        self.assertEqual(client.credentials.get("unit"), 0)
        client._request = lambda m, u, **kw: Resp(200, {"code": 0})
        self.assertTrue(client.update_unit(1)[0])
        self.assertEqual(client.credentials.get("unit"), 1)


# ============================================================================== the calendar, in its REAL shape
REAL_MONTH = {"code": 0, "message": "ok", "data": [
    {"date": "2026-10-27", "isAllFinish": False, "isReservationDay": True, "trainingPlanList": [
        {"type": 5, "title": "Push - Chest & Triceps v2", "isFinish": 0, "isReservation": True, "templateId": 9, "code": "T1",
         "actionList": [{"name": "Dual-Handle Bench Press"}], "trainingId": 0}]},
    {"date": "2026-10-28", "isAllFinish": False, "isReservationDay": False, "trainingPlanList": []},
    {"date": "2026-10-29", "isAllFinish": True, "isReservationDay": False, "trainingPlanList": [
        {"title": "Legs", "isFinish": 1, "finishTime": "2026-10-29 18:02:11"}, {"title": "Core", "isFinish": 0}]},
]}


class CalendarFeedInTheRealShape(unittest.TestCase):
    def test_a_plan_takes_its_days_date(self):
        events = []
        features._walk_events(REAL_MONTH["data"], events)
        self.assertEqual(sorted((d.isoformat(), n) for d, n in events),
                         [("2026-10-27", "Push - Chest & Triceps v2"), ("2026-10-29", "Core"), ("2026-10-29", "Legs")])

    def test_exercise_names_inside_a_plan_are_not_events(self):
        events = []
        features._walk_events(REAL_MONTH["data"], events)
        self.assertNotIn("Dual-Handle Bench Press", [n for _, n in events])

    def test_nodes_with_their_own_date_keep_it(self):
        events = []
        features._walk_events([{"date": "2026-10-01", "plans": [{"title": "A", "scheduleDate": "2026-10-05"}]}], events)
        self.assertEqual([(d.isoformat(), n) for d, n in events], [("2026-10-05", "A")])

    def test_the_ics_feed_contains_the_scheduled_workouts(self):
        c = gym_app.app.test_client()
        with mock.patch.object(gym_app.client, "credentials", {"token": "t"}), \
                mock.patch.object(gym_app.client, "_request", return_value=Resp(200, REAL_MONTH)):
            resp = c.get("/calendar.ics", headers=BASIC)
        text = resp.get_data(as_text=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("DTSTART;VALUE=DATE:20261027", text)
        self.assertIn("Push - Chest & Triceps v2".replace(",", "\\,"), text)
        self.assertGreaterEqual(text.count("BEGIN:VEVENT"), 3)

    def test_an_answer_without_data_is_an_error_not_an_empty_month(self):
        client = make_client(0)
        for body in ({}, {"message": "oops"}, {"code": 0, "data": "x"}):
            client._request = lambda m, u, body=body, **kw: Resp(200, body)
            if body == {"code": 0, "data": "x"} or body in ({}, {"message": "oops"}):
                with self.assertRaises(api_client.UpstreamError, msg=body):
                    client.get_calendar_month("2026-10")
        client._request = lambda m, u, **kw: Resp(200, {"code": 0})
        self.assertEqual(client.get_calendar_month("2026-10"), [])  # an explicit empty success


# ============================================================================== the MCP closure class
class McpToolsDoNotShadowTheirArguments(unittest.TestCase):
    def test_last_workout_runs(self):
        tool = getattr(mcp_server.last_workout, "fn", mcp_server.last_workout)
        with mock.patch.object(mcp_server, "_client", return_value=mock.Mock()), mock.patch.object(mcp_server, "_require_auth"), \
                mock.patch.object(mcp_server, "_detailed_records", return_value=[{"id": 1}, {"id": 2}]) as recs, \
                mock.patch.object(mcp_server, "fetch_session_detail", side_effect=lambda c, r: {"got": r["id"]}):
            self.assertEqual(tool(), {"got": 1})
            self.assertEqual(tool(n=2), {"got": 2})
            self.assertEqual(recs.call_args[0][1], 60)
            self.assertEqual(tool(n=3)["error"], "not_found")
            self.assertEqual(tool(n="x", days=10**9)["error"] if False else tool(n=1, days=10**9), {"got": 1})

    def test_no_nested_function_assigns_to_an_argument_of_its_enclosing_tool(self):
        """The class of bug: `def tool(n): def run(): n = clamp(n)` -> UnboundLocalError on every call."""
        tree = ast.parse(open(os.path.join(ROOT, "mcp_server.py"), encoding="utf-8").read())
        offenders = []
        for outer in ast.walk(tree):
            if not isinstance(outer, ast.FunctionDef):
                continue
            params = {a.arg for a in outer.args.args + outer.args.kwonlyargs}
            for inner in ast.walk(outer):
                if isinstance(inner, ast.FunctionDef) and inner is not outer:
                    declared = {n for g in ast.walk(inner) if isinstance(g, (ast.Nonlocal, ast.Global)) for n in g.names}
                    stored = {t.id for t in ast.walk(inner) if isinstance(t, ast.Name) and isinstance(t.ctx, ast.Store)}
                    for name in (stored & params) - declared:
                        offenders.append((outer.name, inner.name, name))
        self.assertEqual(offenders, [])


# ============================================================================== program payloads are bounded first
class ProgramPayloadsAreBoundedBeforeAllocation(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(features, "resolve_exercise",
                                    return_value={"groupId": 7, "matched_name": "P", "muscle": "chest", "confidence": "high"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def day(self, **e):
        return {"name": "D", "exercises": [{"name": "P", "primary": "chest", "sets": 3, "reps": 8, "load": 0, **e}]}

    def test_huge_set_counts_are_refused_without_building_them(self):
        for sets in (21, 10**6, 10**12, -1, "abc", 1.5e30):
            with self.assertRaises((ValueError, OverflowError), msg=repr(sets)) as ctx:
                features.program_day_to_payload([], self.day(sets=sets))
            self.assertNotIn("abc", str(ctx.exception).replace("a whole number", ""))
        out = features.program_day_to_payload([], self.day(sets=20))
        self.assertEqual(len(out[0][0]["sets"]), 20)

    def test_reps_and_exercise_counts_are_bounded(self):
        for reps in (100, 10**9, -1, "x"):
            with self.assertRaises(ValueError, msg=repr(reps)):
                features.program_day_to_payload([], self.day(reps=reps))
        with self.assertRaises(ValueError):
            features.program_day_to_payload([], {"name": "D", "exercises": [{"name": "P", "sets": 1}] * 51})
        with self.assertRaises(ValueError):
            features.program_day_to_payload([], {"name": "D", "exercises": "nope"})

    def test_the_endpoint_answers_400_for_a_hostile_program(self):
        c = gym_app.app.test_client()
        client = mock.Mock()
        client.credentials = {"token": "x"}
        client.get_library.return_value = []
        with mock.patch.object(features, "_client", client):
            resp = c.post("/api/coach/apply", headers=BASIC, json={"program": {"days": [self.day(sets=10**9)]}, "load_unit": "kg"})
        self.assertEqual((resp.status_code, resp.get_json()["error"]), (400, "bad_request"))
        client.save_workout.assert_not_called()


# ============================================================================== the coach avoid-list
class PainExclusionsAreFuzzyAndLoud(unittest.TestCase):
    def prescribed(self, **kw):
        prog = coach.generate_program(goal="muscle", days_per_week=4, experience="intermediate", **kw)
        return prog, [e["name"].lower() for d in prog["days"] for e in d["exercises"]]

    def test_partial_trailing_space_and_case_all_exclude(self):
        base, names = self.prescribed()
        victim = next(n for n in names if "press" in n)
        word = " ".join(victim.split()[-2:])  # e.g. "chest press"
        for entry in (word, word.upper(), f"  {victim}  ", victim.title()):
            _, after = self.prescribed(feedback={"avoid": [entry]})
            self.assertNotIn(victim, after, entry)

    def test_short_entries_only_match_exactly(self):
        _, names = self.prescribed()
        self.assertTrue(any("row" in n and n != "row" for n in coach_names()), "the catalog must have a name containing 'row'")
        _, after = self.prescribed(feedback={"avoid": ["row"]})  # 3 characters: matches an exact name only, never a substring
        self.assertEqual(sorted(after), sorted(names))

    def test_entries_that_match_nothing_are_reported(self):
        real = coach.CATALOG[0]["name"]
        prog, _ = self.prescribed(feedback={"avoid": ["Zottman Curl Of Doom", real]})
        self.assertEqual(prog["avoid_unmatched"], ["zottman curl of doom"])
        self.assertEqual(self.prescribed()[0]["avoid_unmatched"], [])

    def test_the_equipment_filter_never_falls_back_to_gear_the_user_lacks(self):
        prog = coach.generate_program(goal="muscle", days_per_week=4, experience="intermediate",
                                      available_accessories=["zzz-not-owned"])
        catalog = {e["name"]: e["accessory"] for e in coach.CATALOG}
        for d in prog["days"]:
            for e in d["exercises"]:
                self.assertEqual(catalog[e["name"]], "none", e["name"])


# ============================================================================== readiness gaps are visible
class ReadinessGapsAreVisible(unittest.TestCase):
    NOW = datetime(2026, 10, 26, 21, 0, tzinfo=timezone.utc)

    def plan(self, readings):
        import synth
        from harness.normalize import session_header
        day = self.NOW.date() - timedelta(days=2)
        rec = synth.history_record(1, synth.ts_ms(day.year, day.month, day.day), title="Push")
        return planner.todays_plan(sessions=[session_header(rec)], readings=readings, now=self.NOW, tz="UTC")

    def test_a_stale_categorical_reading_does_not_crash_the_planner(self):
        stale = Reading("hrv_vs_baseline", "below", "manual", self.NOW - timedelta(hours=19))
        fresh = Reading("subjective", 5.0, "manual", self.NOW - timedelta(minutes=5))
        plan = self.plan([stale, fresh])
        self.assertIn("ignored stale hrv_vs_baseline below", " | ".join(plan.warnings))

    def test_whoop_notes_land_in_the_plan_warnings(self):
        now = datetime.now(timezone.utc)

        class Fake:
            def fetch_readings(self):
                return [Reading("sleep_hours", 7.5, "whoop", now - timedelta(hours=1))], ["latest recovery is PENDING_SCORE: not scored yet"]

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(wc, "default_client", return_value=Fake()), mock.patch.object(tracer, "load_recordings", return_value=[]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tracer.main(["--recordings", "/x", "--whoop-live", "--json"])
        self.assertEqual(code, 0, out.getvalue() + err.getvalue())
        warnings = json.loads(out.getvalue())["warnings"]
        self.assertTrue(any(w.startswith("whoop: latest recovery is PENDING_SCORE") for w in warnings), warnings)


# ============================================================================== pages and small hardening
@unittest.skipUnless(NODE, "node is required")
class HistoryPageShowsOutagesAndTheMachineLoad(unittest.TestCase):
    HARNESS = r"""
const vm = require('vm');
const els = {};
const mk = id => (els[id] ??= {textContent: '', innerHTML: '', value: id.includes('Date') ? '2026-10-01' : '',
  classList: {add() {}, remove() {}, contains() { return false; }}, style: {}, addEventListener() {}});
globalThis.document = {getElementById: mk, querySelector: () => mk('q'), querySelectorAll: () => [], addEventListener() {}, createElement: () => mk('c')};
globalThis.window = {location: {}};
globalThis.alert = () => {};
let rendered = false;
globalThis.fetch = async () => ({ok: false, status: 502, json: async () => ({error: 'history_unavailable', detail: 'UpstreamError'})});
vm.runInThisContext(%(script)s + '\nglobalThis.__rendered = () => typeof historyData !== "undefined" && historyData && !historyData.error;');
(async () => { await fetchHistory(); console.log(JSON.stringify({empty: els.emptyState.textContent, rendered: globalThis.__rendered()})); })();
"""

    def test_a_502_is_an_error_message_not_no_workouts(self):
        with mock.patch.object(gym_app.client, "credentials", {"token": "t", "unit": 0}):
            html = gym_app.app.test_client().get("/history", headers=BASIC).get_data(as_text=True)
        scripts = [s for s in __import__("re").findall(r"<script[^>]*>(.*?)</script>", html, __import__("re").S) if "async function fetchHistory" in s]
        self.assertEqual(len(scripts), 1)
        out = subprocess.run([NODE, "-e", self.HARNESS % {"script": json.dumps(scripts[0])}], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr[-600:])
        got = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertIn("Could not load history", got["empty"])
        self.assertIn("history_unavailable", got["empty"])
        self.assertFalse(got["rendered"])

    def test_the_detail_modal_prefers_the_machine_total(self):
        src = open(os.path.join(ROOT, "templates", "history.html"), encoding="utf-8").read()
        block = src[src.index("let wArr = null;"):src.index("if (wArr) {")]
        self.assertLess(block.index("detail.weights && detail.weights.length > 0"), block.index("detail.leftWeights"))


class LoginAndListenerHardening(unittest.TestCase):
    def test_a_failed_login_restores_the_previous_region(self):
        c = gym_app.app.test_client()
        before = (gym_app.client.region, gym_app.client.host, gym_app.client.base_url)
        other = "EU" if before[0] != "EU" else "Global"
        with mock.patch.dict(os.environ, {"GYM_ALLOW_WRITES": "1"}), \
                mock.patch.object(gym_app.client, "login", return_value=(False, "bad password", {})):
            c.post("/login", headers=BASIC, data={"email": "a@b.c", "password": "x", "region": other})
        self.assertEqual((gym_app.client.region, gym_app.client.host, gym_app.client.base_url), before)

    def test_a_successful_login_keeps_the_new_region(self):
        c = gym_app.app.test_client()
        before = (gym_app.client.region, gym_app.client.host, gym_app.client.base_url)
        other = "EU" if before[0] != "EU" else "Global"
        try:
            with mock.patch.dict(os.environ, {"GYM_ALLOW_WRITES": "1"}), \
                    mock.patch.object(gym_app.client, "login", return_value=(True, "ok", {})):
                c.post("/login", headers=BASIC, data={"email": "a@b.c", "password": "x", "region": other})
            self.assertEqual(gym_app.client.region, other)
        finally:
            gym_app.client.region, gym_app.client.host, gym_app.client.base_url = before

    def test_the_callback_handler_has_a_socket_timeout(self):
        self.assertIsNotNone(wc._Callback.timeout)
        self.assertLessEqual(wc._Callback.timeout, 10)

    def test_a_silent_connection_does_not_block_the_real_callback(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        seen = {}

        class Http:
            def post(self, url, data=None, **kw):
                return Resp(200, {"access_token": "a", "refresh_token": "r", "expires_in": 3600,
                                  "scope": "offline read:recovery read:sleep"})

        class Store:
            path = "unused"

            def save(self, tokens):
                seen["saved"] = True

        silent = []

        def open_url(url):
            from urllib.parse import parse_qs, urlparse
            state = parse_qs(urlparse(url).query)["state"][0]
            silent.append(socket.create_connection(("127.0.0.1", port)))  # connects, then says nothing
            threading.Thread(target=lambda: urllib.request.urlopen(
                f"http://127.0.0.1:{port}/callback?code=C&state={state}", timeout=10).read(), daemon=True).start()

        with mock.patch.object(wc._Callback, "timeout", 0.5):
            wc.run_consent(wc.AppCredentials("cid", "secret"), Store(), open_url=open_url, port=port, timeout_s=6, http=Http())
        silent[0].close()
        self.assertTrue(seen["saved"])

    def test_a_bad_port_is_a_one_line_error(self):
        creds = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
        creds.write("WHOOP_CLIENT_ID=11111111-1111-1111-1111-111111111111\nWHOOP_CLIENT_SECRET=" + "a" * 64 + "\n")
        creds.close()
        os.chmod(creds.name, 0o600)
        self.addCleanup(os.unlink, creds.name)
        for port in ("0", "70000", "-1"):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(whoop_auth.main(["--creds", creds.name, "--port", port]), 1, port)
            self.assertIn("--port", err.getvalue())


if __name__ == "__main__":
    unittest.main()
