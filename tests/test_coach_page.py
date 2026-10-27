"""Coach page backend: readiness provenance, the WHOOP soft veto, the reduce-only overlay, preferences, avoid-list removal."""
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

import app as gym_app  # noqa: E402
import coach  # noqa: E402
import features  # noqa: E402
import state_io  # noqa: E402

BASIC = {"Authorization": "Basic " + base64.b64encode(f"gym:{TOKEN}".encode()).decode()}
CROSS = {**BASIC, "Sec-Fetch-Site": "cross-site", "Origin": "https://evil.example"}


def fresh():
    return tempfile.mkdtemp(prefix="gym-coach-")


def stamp(hours_ago):
    return state_io.utc_stamp(datetime.now(timezone.utc) - timedelta(hours=hours_ago))


class Server(unittest.TestCase):
    def setUp(self):
        self.d = fresh()
        self.c = gym_app.app.test_client()
        p = mock.patch.object(features, "_client", mock.Mock(data_dir=self.d, credentials={"token": "x", "unit_confirmed": True, "unit": 1}))
        p.start()
        self.addCleanup(p.stop)

    def stored(self):
        with open(os.path.join(self.d, "readiness.json")) as f:
            return json.load(f)


class Provenance(Server):
    def test_source_is_stored_beside_the_field_timestamp(self):
        keep = features.save_readiness({"whoop_recovery": 61, "sleep_hours": 7, "source": "whoop"}, self.d)
        self.assertEqual(keep["field_source"], {"whoop_recovery": "whoop", "sleep_hours": "whoop"})
        self.assertEqual(set(keep["field_updated"]), {"whoop_recovery", "sleep_hours"})
        self.assertNotIn("source", keep)

    def test_a_typo_in_the_source_is_refused_and_nothing_changes(self):
        features.save_readiness({"whoop_recovery": 61, "source": "whoop"}, self.d)
        before = self.stored()
        for bad in ("Whoop", "garmin", "", None, 1, ["whoop"], "whoop ", "apple_health"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                features.save_readiness({"sleep_hours": 4, "source": bad}, self.d)
            resp = self.c.post("/api/coach/readiness", headers=BASIC, json={"sleep_hours": 4, "source": bad})
            self.assertEqual((resp.status_code, resp.get_json()["error"]), (400, "invalid_readiness"), bad)
        self.assertEqual(self.stored(), before)

    def test_whoop_cannot_claim_fields_it_does_not_supply(self):
        for field, value in (("subjective", 4), ("hrv_vs_baseline", "below"), ("rhr_delta_bpm", 3), ("acwr", 1.2)):
            with self.assertRaises(ValueError, msg=field):
                features.save_readiness({field: value, "source": "whoop"}, self.d)
        features.save_readiness({"subjective": 4, "source": "manual"}, self.d)  # manual can supply anything

    def test_misspelled_provenance_keys_are_unknown_fields(self):
        for key in ("Source", "sources", "field_source", "provenance"):
            with self.assertRaises(ValueError, msg=key):
                features.save_readiness({"whoop_recovery": 50, key: "whoop"}, self.d)

    def test_source_is_refused_on_the_stateless_routes(self):
        for path in ("/api/coach/autoregulate", "/api/coach/program"):
            body = {"whoop_recovery": 50, "source": "whoop"} if "auto" in path else {"readiness": {"whoop_recovery": 50, "source": "whoop"}}
            self.assertEqual(self.c.post(path, headers=BASIC, json=body).status_code, 400, path)

    def test_a_later_post_without_a_source_drops_the_old_source_of_that_field_only(self):
        features.save_readiness({"whoop_recovery": 61, "sleep_hours": 7, "source": "whoop"}, self.d)
        features.save_readiness({"sleep_hours": 5}, self.d)
        self.assertEqual(self.stored()["field_source"], {"whoop_recovery": "whoop"})
        features.save_readiness({"whoop_recovery": 40}, self.d)
        self.assertNotIn("field_source", self.stored())  # nothing left to say: the file looks like an older version's

    def test_an_old_file_without_provenance_or_field_stamps_still_works(self):
        state_io.atomic_write_json(os.path.join(self.d, "readiness.json"), {"whoop_recovery": 80, "updated": stamp(2)})
        body = self.c.get("/api/coach/readiness", headers=BASIC).get_json()
        sig = body["signals"][0]
        self.assertEqual((sig["key"], sig["value"], sig["source"], sig["stale"]), ("whoop_recovery", 80, None, False))
        self.assertEqual(body["adjustment"]["band"], "high")
        keep = features.save_readiness({"sleep_hours": 8}, self.d)  # and it is migrated, not broken, by the next post
        self.assertEqual(keep["whoop_recovery"], 80)

    def test_a_hand_edited_file_cannot_inject_a_source_or_a_value(self):
        state_io.atomic_write_json(os.path.join(self.d, "readiness.json"), {
            "whoop_recovery": 80, "sleep_hours": "<img src=x onerror=1>", "subjective": 99, "updated": stamp(1),
            "field_source": {"whoop_recovery": "<script>", "sleep_hours": "whoop", "subjective": "whoop", "evil": "manual"}})
        sigs = self.c.get("/api/coach/readiness", headers=BASIC).get_json()["signals"]
        self.assertEqual([(s["key"], s["source"]) for s in sigs], [("whoop_recovery", None)])  # junk values and unknown sources dropped

    def test_each_signal_carries_its_own_age_and_stale_flag(self):
        state_io.atomic_write_json(os.path.join(self.d, "readiness.json"), {
            "whoop_recovery": 30, "sleep_hours": 6, "updated": stamp(1),
            "field_updated": {"whoop_recovery": stamp(40), "sleep_hours": stamp(1)}, "field_source": {"whoop_recovery": "whoop", "sleep_hours": "manual"}})
        body = self.c.get("/api/coach/readiness", headers=BASIC).get_json()
        by = {s["key"]: s for s in body["signals"]}
        self.assertTrue(by["whoop_recovery"]["stale"])
        self.assertAlmostEqual(by["whoop_recovery"]["age_hours"], 40, delta=0.2)
        self.assertFalse(by["sleep_hours"]["stale"])
        self.assertEqual(body["max_age_hours"], 36)
        self.assertNotIn("Whoop recovery", " ".join(body["adjustment"]["signals"]))  # the stale one is ignored by the coach
        self.assertEqual(body["adjustment"]["band"], "moderate")  # 6 h of sleep alone

    def test_the_36_hour_boundary(self):
        for hours, stale in ((35.5, False), (36.5, True)):
            state_io.atomic_write_json(os.path.join(self.d, "readiness.json"), {
                "whoop_recovery": 30, "updated": stamp(hours), "field_updated": {"whoop_recovery": stamp(hours)}})
            sig = self.c.get("/api/coach/readiness", headers=BASIC).get_json()["signals"][0]
            self.assertEqual(sig["stale"], stale, hours)

    def test_measured_at_is_validated(self):
        now = datetime.now(timezone.utc)
        for bad in ({"whoop_recovery": now + timedelta(hours=2)}, {"whoop_recovery": datetime.now()}, {"sleep_hours": now},
                    {"whoop_recovery": "2026-01-01"}):
            with self.assertRaises(ValueError, msg=str(bad)):
                features.save_readiness({"whoop_recovery": 50}, self.d, measured_at=bad)

    def test_the_post_answer_includes_signals_and_adjustment(self):
        body = self.c.post("/api/coach/readiness", headers=BASIC, json={"whoop_recovery": 20, "source": "manual"}).get_json()
        self.assertEqual(body["adjustment"]["band"], "low")
        self.assertEqual(body["signals"][0]["source"], "manual")


class SoftVeto(unittest.TestCase):
    def test_a_high_whoop_score_never_raises_volume_or_intensity(self):
        for score in (67, 80, 100):
            out = coach.autoregulate({"whoop_recovery": score})
            self.assertEqual((out["band"], out["volume_pct"], out["intensity_pct"], out["session"]), ("high", 0, 0, "as_planned"), score)
            self.assertTrue(any("soft veto" in o for o in out["overrides"]))
            self.assertIn("never adds", out["message"])

    def test_whoop_alongside_other_high_signals_still_does_not_raise(self):
        out = coach.autoregulate({"whoop_recovery": 95, "sleep_hours": 9, "subjective": 5, "hrv_vs_baseline": "above"})
        self.assertEqual((out["volume_pct"], out["intensity_pct"]), (0, 0))

    def test_a_low_whoop_score_still_cuts_and_most_conservative_still_wins(self):
        self.assertEqual(coach.autoregulate({"whoop_recovery": 20})["volume_pct"], -40)
        self.assertEqual(coach.autoregulate({"whoop_recovery": 95, "subjective": 1})["band"], "low")

    def test_the_overlay_never_raises_whatever_it_is_told(self):
        program = coach.generate_program("muscle", 3, "intermediate", one_rm={"chest": 100, "back": 100, "quads": 100})
        for auto in ({"volume_pct": 50, "intensity_pct": 30, "rir_min": 0, "band": "high"}, {"volume_pct": 10, "intensity_pct": 3, "rir_min": 1, "band": "high"}, {}):
            out = coach.apply_readiness(program, auto)
            self.assertEqual(out["days"], program["days"], auto)
            self.assertEqual(out["readiness_applied"]["adjustments"], [])
            # the record must not claim a raise either: the clamp to zero is what the page shows as "no changes"
            self.assertEqual((out["readiness_applied"]["volume_pct"], out["readiness_applied"]["intensity_pct"]), (0, 0), auto)

    def test_a_low_band_trims_sets_and_load_and_adds_reserve_and_lists_every_change(self):
        program = coach.generate_program("muscle", 3, "intermediate", one_rm={"chest": 100, "back": 100, "quads": 100, "hamstrings": 100})
        out = coach.apply_readiness(program, coach.autoregulate({"whoop_recovery": 20}))
        changes = out["readiness_applied"]["adjustments"]
        self.assertTrue(changes)
        before = {(di, e["name"]): e for di, d in enumerate(program["days"]) for e in d["exercises"]}
        after = {(di, e["name"]): e for di, d in enumerate(out["days"]) for e in d["exercises"]}
        for key, e in before.items():
            a = after[key]
            self.assertLessEqual(a["sets"], e["sets"])
            self.assertGreaterEqual(a["sets"], 1)
            self.assertLessEqual(a["load"] or 0, e["load"] or 0)
            self.assertGreaterEqual(a["rir"], e["rir"])
            self.assertEqual(set(a), set(e))  # no new exercise keys: apply_program refuses unknown ones
        self.assertEqual(sum(sum(e["sets"] for e in d["exercises"]) for d in out["days"]), sum(out["weekly_sets_per_muscle"].values()))
        first = next(c for c in changes if c["field"] == "sets")
        self.assertEqual(after[(first["day"], first["exercise"])]["sets"], first["to"])
        self.assertIn("load", {c["field"] for c in changes})
        self.assertIn("rir", {c["field"] for c in changes})
        self.assertEqual(out["readiness_applied"]["session"], "reduce_or_recover")
        self.assertTrue(out["readiness_applied"]["note"])
        self.assertEqual(program["days"][0]["exercises"][0]["sets"], before[(0, program["days"][0]["exercises"][0]["name"])]["sets"])  # input untouched

    def test_the_program_route_applies_stored_readiness_only_when_asked(self):
        d = fresh()
        c = gym_app.app.test_client()
        with mock.patch.object(features, "_client", mock.Mock(data_dir=d, credentials={"token": "x"})):
            features.save_readiness({"whoop_recovery": 15, "source": "whoop"}, d)
            plain = c.post("/api/coach/program", headers=BASIC, json={"goal": "muscle", "one_rm": {"chest": 100}}).get_json()
            self.assertNotIn("readiness_applied", plain)
            adj = c.post("/api/coach/program", headers=BASIC, json={"goal": "muscle", "one_rm": {"chest": 100}, "apply_readiness": True}).get_json()
            self.assertTrue(adj["readiness_applied"]["adjustments"])
            for flag in ("true", 1, "yes"):
                self.assertNotIn("readiness_applied", c.post("/api/coach/program", headers=BASIC, json={"apply_readiness": flag}).get_json())
            features.save_readiness({"whoop_recovery": 95, "source": "whoop"}, d)
            high = c.post("/api/coach/program", headers=BASIC, json={"goal": "muscle", "one_rm": {"chest": 100}, "apply_readiness": True}).get_json()
            self.assertEqual(high["readiness_applied"]["adjustments"], [])
            self.assertEqual(high["days"], plain["days"])

    def test_the_recovery_principle_no_longer_says_green_means_push(self):
        text = coach.principles("recovery")["recovery"]
        self.assertNotIn("green→push", text)
        self.assertIn("soft veto", text)


class Preferences(Server):
    GOOD = {"goal": "strength", "days_per_week": 3, "experience": "beginner", "one_rm": {"Cable chest press": 80.5, "back": 90}, "load_unit": "kg"}

    def test_round_trip_and_reset(self):
        self.assertEqual(self.c.get("/api/coach/preferences", headers=BASIC).get_json(), {"preferences": {}, "error": None})
        self.assertEqual(self.c.post("/api/coach/preferences", headers=BASIC, json=self.GOOD).get_json()["preferences"], self.GOOD)
        self.assertEqual(self.c.get("/api/coach/preferences", headers=BASIC).get_json()["preferences"], self.GOOD)
        self.assertEqual(self.c.delete("/api/coach/preferences", headers=BASIC).get_json()["reset"], True)
        self.assertEqual(self.c.get("/api/coach/preferences", headers=BASIC).get_json()["preferences"], {})
        self.assertEqual(self.c.delete("/api/coach/preferences", headers=BASIC).status_code, 200)  # resetting twice is fine

    def test_every_bad_value_is_a_400_that_names_the_field_and_changes_nothing(self):
        self.c.post("/api/coach/preferences", headers=BASIC, json=self.GOOD)
        bad = [{"goal": "bulk"}, {"goal": 3}, {"days_per_week": 0}, {"days_per_week": 6}, {"days_per_week": True}, {"days_per_week": "4"},
               {"experience": "expert"}, {"load_unit": "stone"}, {"one_rm": {"chest": 80}}, {"one_rm": {"chest": -5}, "load_unit": "kg"},
               {"one_rm": {"chest": float("nan")}, "load_unit": "kg"}, {"one_rm": {"chest": True}, "load_unit": "kg"},
               {"one_rm": {"chest": "80"}, "load_unit": "kg"}, {"one_rm": {"chest": 5000}, "load_unit": "kg"}, {"one_rm": {"???": 5}, "load_unit": "kg"},
               {"one_rm": {"x" * 200: 5}, "load_unit": "kg"}, {"one_rm": [1], "load_unit": "kg"},
               {"one_rm": {f"e{i}": 5 for i in range(40)}, "load_unit": "kg"}, {"Goal": "muscle"}, {"typo": 1}]
        for body in bad:
            resp = self.c.post("/api/coach/preferences", headers=BASIC, data=json.dumps(body, allow_nan=True), content_type="application/json")
            self.assertEqual((resp.status_code, resp.get_json()["error"]), (400, "invalid_preferences"), body)
        self.assertEqual(self.c.post("/api/coach/preferences", headers=BASIC, json=[1]).status_code, 400)
        self.assertEqual(self.c.get("/api/coach/preferences", headers=BASIC).get_json()["preferences"], self.GOOD)

    def test_an_empty_object_is_nothing_remembered(self):
        self.assertEqual(self.c.post("/api/coach/preferences", headers=BASIC, json={}).get_json()["preferences"], {})

    def test_a_damaged_file_is_reported_and_moved_aside_by_the_next_save(self):
        path = os.path.join(self.d, "coach_prefs.json")
        for text in ("{not json", "[1, 2]", json.dumps({"goal": "bulk"})):
            with open(path, "w") as f:
                f.write(text)
            got = self.c.get("/api/coach/preferences", headers=BASIC).get_json()
            self.assertEqual((got["preferences"], bool(got["error"])), ({}, True), text)
        self.assertEqual(self.c.post("/api/coach/preferences", headers=BASIC, json=self.GOOD).status_code, 200)
        self.assertTrue([n for n in os.listdir(self.d) if n.startswith("coach_prefs.json.corrupt-")])
        self.assertEqual(self.c.get("/api/coach/preferences", headers=BASIC).get_json()["preferences"], self.GOOD)

    def test_the_constants_match_the_engine(self):
        self.assertEqual(set(state_io.PREF_GOALS), set(coach.GOALS))
        self.assertEqual(set(state_io.PREF_EXPERIENCE), set(coach.EXPERIENCE))

    def test_they_are_local_state_and_cross_site_writes_are_refused(self):
        self.assertEqual(self.c.post("/api/coach/preferences", headers=BASIC, json=self.GOOD).status_code, 200)  # writes are off
        for method in ("post", "delete"):
            resp = getattr(self.c, method)("/api/coach/preferences", headers=CROSS, json=self.GOOD)
            self.assertEqual((resp.status_code, resp.get_json()["error"]), (403, "cross_site_request_refused"), method)
        self.assertEqual(self.c.post("/api/coach/preferences", json=self.GOOD).status_code, 401)
        self.assertEqual(self.c.get("/api/coach/preferences", headers=BASIC).get_json()["preferences"], self.GOOD)

    def test_the_remembered_values_make_the_same_program_as_typing_them(self):
        prefs = self.c.post("/api/coach/preferences", headers=BASIC, json=self.GOOD).get_json()["preferences"]
        a = self.c.post("/api/coach/program", headers=BASIC, json={"goal": prefs["goal"], "days_per_week": prefs["days_per_week"],
                                                                   "experience": prefs["experience"], "one_rm": prefs["one_rm"]}).get_json()
        self.assertEqual((a["goal"], a["days_per_week"], a["experience"]), ("strength", 3, "beginner"))


class AvoidList(Server):
    def avoid(self):
        return self.c.get("/api/coach/feedback", headers=BASIC).get_json()["profile"]["avoid"]

    def test_add_list_remove(self):
        self.c.post("/api/coach/feedback", headers=BASIC, json={"avoid": "Lat pulldown"})
        self.c.post("/api/coach/feedback", headers=BASIC, json={"pain": True, "exercise": "Cable curl"})
        self.assertEqual(self.avoid(), ["Cable curl", "Lat pulldown"])
        resp = self.c.delete("/api/coach/feedback?avoid=LAT%20pulldown%20", headers=BASIC)  # the engine's own name matching: case and spacing
        self.assertEqual((resp.status_code, resp.get_json()["removed"]), (200, True))
        self.assertEqual(self.avoid(), ["Cable curl"])
        self.assertEqual(self.c.delete("/api/coach/feedback?avoid=Cable%20curl", headers=BASIC).status_code, 200)
        self.assertEqual(self.avoid(), [])

    def test_removing_something_that_is_not_listed_is_a_404_and_changes_nothing(self):
        self.c.post("/api/coach/feedback", headers=BASIC, json={"avoid": "Lat pulldown"})
        self.assertEqual(self.c.delete("/api/coach/feedback?avoid=Squat", headers=BASIC).status_code, 404)
        self.assertEqual(self.avoid(), ["Lat pulldown"])

    def test_bad_names_are_refused(self):
        for q in ("", "?avoid=", "?avoid=%3F%3F%3F", "?avoid=" + "x" * 300, "?other=Squat"):
            resp = self.c.delete("/api/coach/feedback" + q, headers=BASIC)
            self.assertEqual(resp.status_code, 400, q)

    def test_removal_keeps_the_session_signals_of_a_report_that_carried_them(self):
        self.c.post("/api/coach/feedback", headers=BASIC, json={"avoid": "Squat", "rpe": 9, "difficulty": "too_hard"})
        self.assertEqual(self.c.delete("/api/coach/feedback?avoid=Squat", headers=BASIC).status_code, 200)
        prof = self.c.get("/api/coach/feedback", headers=BASIC).get_json()["profile"]
        self.assertEqual((prof["avoid"], prof["avg_rpe"], prof["difficulty_signal"]), ([], 9, "too_hard"))

    def test_removing_one_of_two_exclusions_in_one_report_keeps_the_other(self):
        self.c.post("/api/coach/feedback", headers=BASIC, json={"pain": True, "exercise": "Squat", "avoid": "Lunge"})
        self.assertEqual(sorted(self.avoid()), ["Lunge", "Squat"])
        self.c.delete("/api/coach/feedback?avoid=Squat", headers=BASIC)
        self.assertEqual(self.avoid(), ["Lunge"])

    def test_a_removed_exercise_comes_back_into_programs(self):
        self.c.post("/api/coach/feedback", headers=BASIC, json={"avoid": "Cable chest press"})
        names = lambda p: [e["name"] for d in p["days"] for e in d["exercises"]]
        before = self.c.post("/api/coach/program", headers=BASIC, json={"days_per_week": 2}).get_json()
        self.assertNotIn("Cable chest press", names(before))
        self.c.delete("/api/coach/feedback?avoid=Cable%20chest%20press", headers=BASIC)
        after = self.c.post("/api/coach/program", headers=BASIC, json={"days_per_week": 2}).get_json()
        self.assertIn("Cable chest press", names(after))

    def test_a_cross_site_delete_is_refused(self):
        self.c.post("/api/coach/feedback", headers=BASIC, json={"avoid": "Squat"})
        resp = self.c.delete("/api/coach/feedback?avoid=Squat", headers=CROSS)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.avoid(), ["Squat"])


class StatusAndPage(Server):
    def test_status_is_booleans_and_the_unit_only_when_confirmed(self):
        body = self.c.get("/api/coach/status", headers=BASIC).get_json()
        self.assertEqual(set(body), {"authenticated", "writes_enabled", "unit_confirmed", "account_unit"})
        self.assertEqual((body["authenticated"], body["writes_enabled"], body["unit_confirmed"], body["account_unit"]), (True, False, True, "lb"))
        with mock.patch.object(features, "_client", mock.Mock(data_dir=self.d, credentials={"unit": 1})):
            body = self.c.get("/api/coach/status", headers=BASIC).get_json()
            self.assertEqual((body["authenticated"], body["unit_confirmed"], body["account_unit"]), (False, False, None))
        with mock.patch.dict(os.environ, {"GYM_ALLOW_WRITES": "1"}):
            self.assertTrue(self.c.get("/api/coach/status", headers=BASIC).get_json()["writes_enabled"])
        self.assertNotIn("x", json.dumps(self.c.get("/api/coach/status", headers=BASIC).get_json()).replace('"x"', ""))  # no token echoed

    def test_unresolved_exercises_say_why(self):
        day = {"name": "d", "exercises": [{"name": "Zzz machine", "primary": "chest", "sets": 3, "reps": 8, "load": 0}]}
        _ex, _res, unresolved = features.program_day_to_payload([], day, "kg", False)
        self.assertEqual(unresolved[0]["reason"], "not found in your exercise library")

    def test_the_page_extends_the_layout_and_says_what_whoop_is(self):
        html = self.c.get("/coach", headers=BASIC).get_data(as_text=True)
        self.assertIn('href="/settings"', html)  # the layout's navigation is there
        for text in ("soft veto", "not validated for strength training", "36 hours", "never adds"):
            self.assertIn(text, html)
        self.assertEqual(self.c.get("/coach").status_code, 401)

    def test_the_page_never_embeds_a_secret_or_marks_anything_safe(self):
        src = open(os.path.join(os.path.dirname(__file__), "..", "templates", "coach.html"), encoding="utf-8").read()
        self.assertNotIn("|safe", src)
        self.assertNotIn("Markup(", src)
        html = self.c.get("/coach", headers=BASIC).get_data(as_text=True)
        for needle in (TOKEN, "WHOOP_CLIENT_SECRET", "access_token", "refresh_token"):
            self.assertNotIn(needle, html)
        self.assertEqual(html.count("{% raw"), 0)  # the Jinja raw markers are consumed, not shipped

    def test_the_page_defines_no_helper_the_layout_already_owns(self):
        src = open(os.path.join(os.path.dirname(__file__), "..", "templates", "coach.html"), encoding="utf-8").read()
        for name in ("esc", "jsq"):
            self.assertNotRegex(src, rf"(?:const|let|var|function)\s+{name}\b")  # a second declaration is a SyntaxError that kills the page
        self.assertEqual(sorted(set(__import__("re").findall(r"\{% block (\w+) %\}", src))), ["content", "head"])
        self.assertTrue(src.startswith('{% extends "layout.html" %}'))


if __name__ == "__main__":
    unittest.main()
