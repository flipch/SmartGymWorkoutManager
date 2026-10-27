"""Readiness/feedback persistence, strict apply, calendar feed (review round 12)."""
import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

TOKEN = "t" * 32
os.environ["GYM_API_TOKEN"] = TOKEN
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="gym-test-data-")
os.environ.pop("GYM_AUTH_DISABLED", None)
os.environ.pop("GYM_ALLOW_WRITES", None)
REPO = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, REPO)

import app as gym_app  # noqa: E402
import features  # noqa: E402
import state_io  # noqa: E402

BASIC = {"Authorization": "Basic " + base64.b64encode(f"gym:{TOKEN}".encode()).decode()}


def fresh_dir():
    return tempfile.mkdtemp(prefix="gym-state-")


class ReadinessValidation(unittest.TestCase):
    def test_accepts_the_documented_signals(self):
        got = state_io.validate_readiness({"whoop_recovery": 70, "hrv_vs_baseline": "Below", "rhr_delta_bpm": -2.5,
                                           "sleep_hours": 7.5, "subjective": 4, "acwr": 1.2, "junk": "x"})
        self.assertEqual(got, {"whoop_recovery": 70, "hrv_vs_baseline": "below", "rhr_delta_bpm": -2.5,
                               "sleep_hours": 7.5, "subjective": 4, "acwr": 1.2})

    def test_rejects_out_of_range_and_wrong_types(self):
        bad = [{"whoop_recovery": 101}, {"whoop_recovery": -1}, {"whoop_recovery": "70"}, {"whoop_recovery": True},
               {"whoop_recovery": float("nan")}, {"whoop_recovery": float("inf")}, {"sleep_hours": 25},
               {"sleep_hours": -1}, {"subjective": 0}, {"subjective": 6}, {"acwr": 9}, {"rhr_delta_bpm": 99},
               {"hrv_vs_baseline": "great"}, {"hrv_vs_baseline": 3}, {"hrv_vs_baseline": None}]
        for payload in bad:
            with self.assertRaises(ValueError, msg=str(payload)):
                state_io.validate_readiness(payload)

    def test_boundaries_are_inclusive(self):
        for payload in ({"whoop_recovery": 0}, {"whoop_recovery": 100}, {"sleep_hours": 24}, {"subjective": 1},
                        {"subjective": 5}, {"rhr_delta_bpm": -30}, {"rhr_delta_bpm": 40}, {"acwr": 5}):
            self.assertTrue(state_io.validate_readiness(payload), payload)

    def test_error_names_the_field_not_the_value(self):
        with self.assertRaises(ValueError) as ctx:
            state_io.validate_readiness({"whoop_recovery": 424242})
        self.assertIn("whoop_recovery", str(ctx.exception))
        self.assertNotIn("424242", str(ctx.exception))

    def test_non_object_is_rejected(self):
        for payload in (None, [], "x", 5):
            with self.assertRaises(ValueError):
                state_io.validate_readiness(payload)


class FeedbackValidation(unittest.TestCase):
    def test_accepts_every_documented_field(self):
        got = state_io.validate_feedback({"rpe": 8, "difficulty": "too_hard", "completion": "missed", "soreness": 3,
                                          "pain": True, "avoid": " Lunge ", "exercise": "Squat", "note": "knee",
                                          "workout_code": "ab-12_C", "extra": 1})
        self.assertEqual(got["avoid"], "Lunge")
        self.assertNotIn("extra", got)
        self.assertEqual(set(got), {"rpe", "difficulty", "completion", "soreness", "pain", "avoid", "exercise",
                                    "note", "workout_code"})

    def test_rejects_bad_values(self):
        bad = [{"rpe": 0}, {"rpe": 11}, {"rpe": "8"}, {"rpe": True}, {"soreness": 6}, {"difficulty": "meh"},
               {"pain": "yes"}, {"pain": 1}, {"avoid": ""}, {"avoid": "   "}, {"avoid": 5}, {"avoid": "x" * 121},
               {"note": "x" * 501}, {"note": ["a"]}, {"workout_code": "a b"}, {"workout_code": "x" * 65},
               {"completion": "x" * 41}]
        for payload in bad:
            with self.assertRaises(ValueError, msg=str(payload)):
                state_io.validate_feedback(payload)

    def test_control_characters_are_stripped_from_text(self):
        self.assertEqual(state_io.validate_feedback({"note": "a\x00b\x1b[31mc"})["note"], "ab[31mc")

    def test_length_limits_are_inclusive(self):
        self.assertEqual(len(state_io.validate_feedback({"note": "x" * 500})["note"]), 500)
        self.assertEqual(len(state_io.validate_feedback({"avoid": "x" * 120})["avoid"]), 120)

    def test_empty_payload_is_empty_not_an_error(self):
        self.assertEqual(state_io.validate_feedback({}), {})


class AtomicWrites(unittest.TestCase):
    def test_no_temp_files_and_content_written(self):
        d = fresh_dir()
        state_io.atomic_write_json(os.path.join(d, "x.json"), {"a": 1})
        self.assertEqual(os.listdir(d), ["x.json"])
        self.assertEqual(json.load(open(os.path.join(d, "x.json"))), {"a": 1})

    def test_a_failed_write_keeps_the_old_file_and_leaves_no_temp(self):
        d = fresh_dir()
        path = os.path.join(d, "x.json")
        state_io.atomic_write_json(path, {"old": True})
        with self.assertRaises(TypeError):
            state_io.atomic_write_json(path, {"bad": object()})
        self.assertEqual(json.load(open(path)), {"old": True})
        self.assertEqual(os.listdir(d), ["x.json"])

    def test_file_is_readable_by_the_sibling_container(self):
        d = fresh_dir()
        path = os.path.join(d, "x.json")
        state_io.atomic_write_json(path, {})
        self.assertEqual(os.stat(path).st_mode & 0o044, 0o044)

    def test_lock_excludes_and_times_out(self):
        d = fresh_dir()
        lock = os.path.join(d, "l.lock")
        with state_io.file_lock(lock):
            with self.assertRaises(TimeoutError):
                with state_io.file_lock(lock, timeout=0.1):
                    pass
        with state_io.file_lock(lock, timeout=0.5):  # released afterwards
            pass


class FeedbackStore(unittest.TestCase):
    def test_corrupt_history_is_quarantined_not_destroyed(self):
        d = fresh_dir()
        path = os.path.join(d, "coach_feedback.json")
        with open(path, "w") as fh:
            fh.write("{ not json")
        features.log_feedback({"rpe": 7}, d)
        names = sorted(os.listdir(d))
        self.assertTrue(any(n.startswith("coach_feedback.json.corrupt-") for n in names), names)
        corrupt = next(n for n in names if ".corrupt-" in n)
        self.assertEqual(open(os.path.join(d, corrupt)).read(), "{ not json")
        self.assertEqual(len(json.load(open(path))), 1)

    def test_a_non_list_file_is_quarantined_too(self):
        d = fresh_dir()
        with open(os.path.join(d, "coach_feedback.json"), "w") as fh:
            json.dump({"oops": 1}, fh)
        features.log_feedback({"rpe": 7}, d)
        self.assertTrue(any(".corrupt-" in n for n in os.listdir(d)))

    def test_readers_do_not_move_files(self):
        d = fresh_dir()
        with open(os.path.join(d, "coach_feedback.json"), "w") as fh:
            fh.write("garbage")
        self.assertEqual(features._load_feedback_events(d), [])
        self.assertEqual(os.listdir(d), ["coach_feedback.json"])

    def test_write_failure_is_raised_not_swallowed(self):
        d = fresh_dir()
        with mock.patch.object(state_io, "atomic_write_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                features.log_feedback({"rpe": 7}, d)
        self.assertEqual(features._load_feedback_events(d), [])

    def test_invalid_event_is_not_stored(self):
        d = fresh_dir()
        with self.assertRaises(ValueError):
            features.log_feedback({"rpe": 99}, d)
        self.assertEqual(features._load_feedback_events(d), [])

    def test_nothing_to_log_returns_none(self):
        self.assertIsNone(features.log_feedback({"nonsense": 1}, fresh_dir()))

    def test_concurrent_appends_lose_nothing(self):
        d = fresh_dir()
        errors = []

        def worker(n):
            try:
                for i in range(5):
                    features.log_feedback({"rpe": 5, "note": f"t{n}-{i}"}, d)
            except Exception as err:  # noqa: BLE001
                errors.append(err)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len({e["note"] for e in features._load_feedback_events(d)}), 40)

    def test_history_is_capped_at_100(self):
        d = fresh_dir()
        for i in range(105):
            features.log_feedback({"note": f"n{i}"}, d)
        events = features._load_feedback_events(d)
        self.assertEqual((len(events), events[0]["note"], events[-1]["note"]), (100, "n5", "n104"))

    def test_old_style_timestamps_still_count_as_recent(self):
        d = fresh_dir()
        old_style = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S") + "Z"
        stale = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%S") + "Z"
        state_io.atomic_write_json(os.path.join(d, "coach_feedback.json"),
                                   [{"rpe": 9, "ts": stale}] * 12 + [{"rpe": 5, "ts": old_style}])
        prof = features._feedback_profile(d)
        self.assertEqual((prof["recent_events"], prof["avg_rpe"]), (1, 5))

    def test_endpoint_status_codes(self):
        c = gym_app.app.test_client()
        with mock.patch.object(features, "_client", mock.Mock(data_dir=fresh_dir(), credentials={"token": "x"})):
            ok = c.post("/api/coach/feedback", headers=BASIC, json={"rpe": 7})
            self.assertEqual(ok.status_code, 200)
            bad = c.post("/api/coach/feedback", headers=BASIC, json={"rpe": 70})
            self.assertEqual((bad.status_code, bad.get_json()["error"]), (400, "invalid_feedback"))
            self.assertNotIn("70", bad.get_json()["message"])
            empty = c.post("/api/coach/feedback", headers=BASIC, json={"nonsense": 1})
            self.assertEqual((empty.status_code, empty.get_json()["error"]), (400, "empty"))
            with mock.patch.object(state_io, "atomic_write_json", side_effect=OSError("/secret/path")):
                lost = c.post("/api/coach/feedback", headers=BASIC, json={"rpe": 7})
            self.assertEqual((lost.status_code, lost.get_json()["error"]), (503, "feedback_not_saved"))
            self.assertNotIn("/secret/path", lost.get_data(as_text=True))


class ReadinessStore(unittest.TestCase):
    def test_round_trip_is_validated_and_stamped(self):
        d = fresh_dir()
        keep = features.save_readiness({"whoop_recovery": 55}, d)
        self.assertEqual(set(keep), {"whoop_recovery", "updated", "field_updated"})
        self.assertEqual(features._load_readiness(d), keep)
        with self.assertRaises(ValueError):  # a typo beside a valid field is not silently dropped
            features.save_readiness({"whoop_recovery": 56, "junk": 1}, d)
        self.assertEqual(features._load_readiness(d)["whoop_recovery"], 55)

    def test_invalid_does_not_overwrite_the_previous_reading(self):
        d = fresh_dir()
        features.save_readiness({"whoop_recovery": 55}, d)
        with self.assertRaises(ValueError):
            features.save_readiness({"whoop_recovery": 555}, d)
        self.assertEqual(features._load_readiness(d)["whoop_recovery"], 55)

    def test_empty_is_an_error(self):
        with self.assertRaises(ValueError):
            features.save_readiness({"junk": 1}, fresh_dir())

    def _write(self, d, age_hours, **extra):
        stamp = state_io.utc_stamp(datetime.now(timezone.utc) - timedelta(hours=age_hours))
        state_io.atomic_write_json(os.path.join(d, "readiness.json"), {"whoop_recovery": 90, "updated": stamp, **extra})

    def test_fresh_reading_is_used(self):
        d = fresh_dir()
        self._write(d, 2)
        self.assertEqual(features._fresh_readiness(d)["whoop_recovery"], 90)

    def test_stale_reading_is_ignored(self):
        d = fresh_dir()
        self._write(d, 40)
        self.assertEqual(features._fresh_readiness(d), {})

    def test_the_boundary(self):
        d = fresh_dir()
        self._write(d, 35)
        self.assertTrue(features._fresh_readiness(d))
        self._write(d, 37)
        self.assertFalse(features._fresh_readiness(d))

    def test_future_dated_or_unstamped_readings_are_ignored(self):
        d = fresh_dir()
        self._write(d, -5)
        self.assertEqual(features._fresh_readiness(d), {})
        state_io.atomic_write_json(os.path.join(d, "readiness.json"), {"whoop_recovery": 90})
        self.assertEqual(features._fresh_readiness(d), {})

    def test_legacy_stamp_format_is_read(self):
        d = fresh_dir()
        legacy = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(tzinfo=None).isoformat() + "Z"
        state_io.atomic_write_json(os.path.join(d, "readiness.json"), {"whoop_recovery": 90, "updated": legacy})
        self.assertTrue(features._fresh_readiness(d))

    def test_program_endpoint_ignores_a_stale_reading_and_uses_a_fresh_one(self):
        c = gym_app.app.test_client()
        d = fresh_dir()
        client = mock.Mock(data_dir=d, credentials={"token": "x"})
        with mock.patch.object(features, "_client", client):
            self._write(d, 80, whoop_recovery=5)
            stale = c.post("/api/coach/program", headers=BASIC, json={"goal": "muscle"}).get_json()
            self._write(d, 1, whoop_recovery=5)
            fresh = c.post("/api/coach/program", headers=BASIC, json={"goal": "muscle"}).get_json()
        self.assertFalse(stale.get("autoregulation"))
        self.assertEqual(fresh["autoregulation"]["band"], "low")

    def test_get_reports_staleness_and_does_not_autoregulate_from_it(self):
        c = gym_app.app.test_client()
        d = fresh_dir()
        self._write(d, 80, whoop_recovery=5)
        with mock.patch.object(features, "_client", mock.Mock(data_dir=d, credentials={"token": "x"})):
            body = c.get("/api/coach/readiness", headers=BASIC).get_json()
        self.assertTrue(body["stale"])
        self.assertNotIn("Whoop recovery", json.dumps(body["adjustment"]))

    def test_post_rejects_bad_values_with_400(self):
        c = gym_app.app.test_client()
        with mock.patch.object(features, "_client", mock.Mock(data_dir=fresh_dir(), credentials={"token": "x"})):
            for payload in ({"whoop_recovery": 500}, {"nonsense": 1}, [1, 2]):
                resp = c.post("/api/coach/readiness", headers=BASIC, json=payload)
                self.assertEqual((resp.status_code, resp.get_json()["error"]), (400, "invalid_readiness"), payload)


class StrictApply(unittest.TestCase):
    def setUp(self):
        self.program = {"goal_label": "Muscle", "days": [{"name": "Push", "exercises": []},
                                                          {"name": "Pull", "exercises": []},
                                                          {"name": "Legs", "exercises": []}]}
        self.client = mock.Mock()
        self.client.get_library.return_value = []
        patcher = mock.patch.object(features, "program_day_to_payload",
                                    return_value=([{"groupId": 1, "sets": [{"reps": 10, "weight": 0, "rest": 60}]}], [], []))
        self.client.save_workout.return_value = {"code": 0, "data": {"id": 1}}
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_invalid_day_index_never_falls_back_to_all_days(self):
        for bad in (True, False, -1, 3, 99, "1", 1.0, [], {}):
            with self.assertRaises(ValueError, msg=repr(bad)):
                features.apply_program(self.client, self.program, bad, None, dry_run=False)
        self.client.save_workout.assert_not_called()

    def test_valid_day_index_targets_exactly_one(self):
        out = features.apply_program(self.client, self.program, 2, None, dry_run=False)
        self.assertEqual([r["day_index"] for r in out["results"]], [2])
        self.assertEqual(self.client.save_workout.call_count, 1)

    def test_none_means_all_days(self):
        out = features.apply_program(self.client, self.program, None, None, dry_run=True)
        self.assertEqual([r["day_index"] for r in out["results"]], [0, 1, 2])

    def test_dry_run_saves_nothing(self):
        features.apply_program(self.client, self.program, None, None, dry_run=True)
        self.client.save_workout.assert_not_called()

    def test_malformed_programs_are_rejected(self):
        for program in ({"days": []}, {"days": "x"}, {"days": [1]}, {"days": [{"name": "a"}, "b"]}, {}):
            with self.assertRaises(ValueError, msg=str(program)):
                features.apply_program(self.client, program, None, None)

    def test_explicit_name_gets_the_day_label_when_several_days_are_saved(self):
        out = features.apply_program(self.client, self.program, None, "Block A", dry_run=True)
        self.assertEqual([r["name"] for r in out["results"]], ["Block A — Push", "Block A — Pull", "Block A — Legs"])
        single = features.apply_program(self.client, self.program, 1, "Block A", dry_run=True)
        self.assertEqual(single["results"][0]["name"], "Block A")

    def test_bad_names_are_rejected(self):
        for name in ("", "   ", 5, "x" * 81):
            with self.assertRaises(ValueError, msg=repr(name)):
                features.apply_program(self.client, self.program, 0, name)

    def test_partial_failure_is_reported_and_leaks_no_text(self):
        self.client.save_workout.side_effect = [{"code": 0, "data": {"id": 1}}, RuntimeError("https://api/secret?token=1"), {"code": 0, "data": 3}]
        out = features.apply_program(self.client, self.program, None, None, dry_run=False)
        self.assertEqual((out["created"], out["failed"], out["partial"]), (2, 1, True))
        self.assertEqual(out["results"][1]["error"], "RuntimeError")
        self.assertNotIn("secret", json.dumps(out))

    def test_total_failure_is_not_partial(self):
        self.client.save_workout.side_effect = RuntimeError("x")
        out = features.apply_program(self.client, self.program, None, None, dry_run=False)
        self.assertEqual((out["created"], out["failed"], out["partial"]), (0, 3, False))

    def test_endpoint_maps_bad_input_to_400_and_never_calls_the_vendor(self):
        c = gym_app.app.test_client()
        with mock.patch.object(features, "_client", self.client):
            self.client.credentials = {"token": "x"}
            for body in ({"program": self.program, "day_index": True},
                         {"program": self.program, "day_index": 7},
                         {"program": self.program, "name": ""}):
                resp = c.post("/api/coach/apply", headers=BASIC, json=body)
                self.assertEqual((resp.status_code, resp.get_json()["error"]), (400, "bad_request"), body)
            self.client.save_workout.assert_not_called()


class CalendarFeed(unittest.TestCase):
    NOW = datetime(2026, 10, 26, 12, 0, 0, tzinfo=timezone.utc)

    def test_uid_is_stable_across_processes(self):
        code = ("import features, datetime as d;"
                "e=[(d.date(2026,10,27),'Push, Pull; \\\\ day')];"
                f"print(features._build_ics(e, d.datetime(2026,10,26,12,tzinfo=d.timezone.utc)))")
        outs = []
        for seed in ("1", "2", "3"):
            env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONDONTWRITEBYTECODE": "1"}
            outs.append(subprocess.run([sys.executable, "-B", "-c", code], cwd=REPO, env=env,
                                       capture_output=True, text=True, check=True).stdout)
        self.assertEqual(len(set(outs)), 1)

    def test_uid_differs_per_event_and_is_sha_derived(self):
        ics = features._build_ics([(date(2026, 10, 27), "A"), (date(2026, 10, 27), "B")], self.NOW)
        uids = [l for l in ics.split("\r\n") if l.startswith("UID:")]
        self.assertEqual(len(set(uids)), 2)
        self.assertTrue(all(u.startswith("UID:20261027-") and u.endswith("@smartgym.local") for u in uids))

    def test_duplicates_collapse(self):
        ics = features._build_ics([(date(2026, 10, 27), "A")] * 3, self.NOW)
        self.assertEqual(ics.count("BEGIN:VEVENT"), 1)

    def test_text_escaping(self):
        self.assertEqual(features._ics_text("a\\b,c;d"), "a\\\\b\\,c\\;d")

    def test_cr_and_lf_cannot_inject_properties(self):
        ics = features._build_ics([(date(2026, 10, 27), "x\r\nBEGIN:VEVENT\r\nSUMMARY:evil\rATTENDEE:y\nz")], self.NOW)
        lines = ics.split("\r\n")
        self.assertEqual(lines.count("BEGIN:VEVENT"), 1)  # as a LINE; inside the SUMMARY value it is just text
        self.assertEqual(sum(1 for l in lines if l.startswith("SUMMARY:")), 1)
        for line in lines:
            self.assertFalse(line.startswith(("ATTENDEE", " ATTENDEE", "SUMMARY:evil")), line)

    def test_control_characters_are_dropped(self):
        self.assertEqual(features._ics_text("a\x00b\x07c"), "abc")

    def test_long_lines_fold_at_75_octets_and_round_trip(self):
        title = "é" * 120 + ", end"
        ics = features._build_ics([(date(2026, 10, 27), title)], self.NOW)
        for line in ics.split("\r\n"):
            self.assertLessEqual(len(line.encode("utf-8")), 75, line)
        unfolded = ics.replace("\r\n ", "")
        self.assertIn("SUMMARY:🏋 " + "é" * 120 + "\\, end", unfolded)

    def test_fold_never_splits_a_multibyte_character(self):
        for n in range(60, 90):
            folded = features._ics_fold("SUMMARY:" + "🏋" * n)
            folded.encode("utf-8").decode("utf-8")  # raises if a character was cut
            self.assertEqual(folded.replace("\r\n ", ""), "SUMMARY:" + "🏋" * n)

    def test_dtstamp_is_utc_and_deterministic(self):
        self.assertIn("DTSTAMP:20261026T120000Z", features._build_ics([(date(2026, 10, 27), "A")], self.NOW))

    def test_output_uses_crlf_throughout(self):
        ics = features._build_ics([(date(2026, 10, 27), "A")], self.NOW)
        self.assertNotIn("\n", ics.replace("\r\n", ""))

    def test_endpoint_outage_is_a_503_not_an_empty_calendar(self):
        c = gym_app.app.test_client()
        fake = mock.Mock(credentials={"token": "x"})
        fake.get_calendar_month.side_effect = ConnectionError("down")
        with mock.patch.object(features, "_client", fake):
            resp = c.get("/calendar.ics", headers=BASIC)
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.headers["Retry-After"], "300")
        self.assertNotIn("BEGIN:VCALENDAR", resp.get_data(as_text=True))

    def test_a_single_failed_month_is_also_a_503(self):
        c = gym_app.app.test_client()
        fake = mock.Mock(credentials={"token": "x"})
        fake.get_calendar_month.side_effect = [{"date": "2026-10-27", "title": "Push"}, ConnectionError("down")]
        with mock.patch.object(features, "_client", fake):
            self.assertEqual(c.get("/calendar.ics", headers=BASIC).status_code, 503)

    def test_signed_out_is_a_503_not_an_empty_calendar(self):
        c = gym_app.app.test_client()
        with mock.patch.object(features, "_client", mock.Mock(credentials={})):
            self.assertEqual(c.get("/calendar.ics", headers=BASIC).status_code, 503)

    def test_success_is_a_calendar(self):
        c = gym_app.app.test_client()
        fake = mock.Mock(credentials={"token": "x"})
        fake.get_calendar_month.return_value = {"days": [{"date": "2026-10-27", "title": "Push"}]}
        with mock.patch.object(features, "_client", fake):
            resp = c.get("/calendar.ics", headers=BASIC)
        self.assertEqual((resp.status_code, resp.mimetype), (200, "text/calendar"))
        self.assertIn("SUMMARY:🏋 Push", resp.get_data(as_text=True))


class SurvivorsPinned(unittest.TestCase):
    """One test per mutant that survived the first mutation run."""

    def test_stamps_have_microseconds_and_sort_as_strings(self):
        a = state_io.utc_stamp(datetime(2026, 10, 26, 12, 0, 0, 5, tzinfo=timezone.utc))
        b = state_io.utc_stamp(datetime(2026, 10, 26, 12, 0, 0, 50, tzinfo=timezone.utc))
        self.assertEqual(a, "2026-10-26T12:00:00.000005Z")
        self.assertLess(a, b)

    def test_readiness_store_error_text_does_not_leak(self):
        c = gym_app.app.test_client()
        with mock.patch.object(features, "_client", mock.Mock(data_dir=fresh_dir(), credentials={"token": "x"})):
            with mock.patch.object(state_io, "atomic_write_json", side_effect=OSError("/secret/path")):
                resp = c.post("/api/coach/readiness", headers=BASIC, json={"whoop_recovery": 50})
        self.assertEqual((resp.status_code, resp.get_json()["error"]), (500, "could_not_store"))
        self.assertNotIn("/secret/path", resp.get_data(as_text=True))

    def test_apply_endpoint_error_text_does_not_leak(self):
        c = gym_app.app.test_client()
        program = {"days": [{"name": "A", "exercises": []}]}
        with mock.patch.object(features, "_client", mock.Mock(credentials={"token": "x"})), \
                mock.patch.object(features, "apply_program", side_effect=RuntimeError("https://x/secret?token=1")):
            resp = c.post("/api/coach/apply", headers=BASIC, json={"program": program})
        self.assertEqual((resp.status_code, resp.get_json()["error"]), (502, "apply_failed"))
        self.assertEqual(resp.get_json()["message"], "RuntimeError")

    def test_ascii_lines_fold_at_exactly_75_octets(self):
        folded = features._ics_fold("SUMMARY:" + "a" * 200).split("\r\n")
        self.assertEqual(len(folded[0].encode()), 75)
        for cont in folded[1:-1]:
            self.assertTrue(cont.startswith(" "))
            self.assertEqual(len(cont.encode()), 75)  # one leading space + 74 octets
        self.assertLessEqual(len(folded[-1].encode()), 75)

    def test_dtstamp_is_utc_even_when_the_host_is_not(self):
        import time
        old = os.environ.get("TZ")
        os.environ["TZ"] = "Pacific/Kiritimati"  # UTC+14: any naive-local stamp is a day off
        time.tzset()
        try:
            ics = features._build_ics([(date(2026, 10, 27), "A")])
        finally:
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            time.tzset()
        stamp = next(l for l in ics.split("\r\n") if l.startswith("DTSTAMP:"))[8:]
        parsed = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        self.assertLess(abs((datetime.now(timezone.utc) - parsed).total_seconds()), 120)

    def test_mcp_generate_program_ignores_stale_readiness_and_uses_fresh(self):
        import mcp_server
        d = fresh_dir()
        raw = getattr(mcp_server.generate_program, "fn", mcp_server.generate_program)
        write = lambda hours: state_io.atomic_write_json(os.path.join(d, "readiness.json"), {
            "whoop_recovery": 5, "updated": state_io.utc_stamp(datetime.now(timezone.utc) - timedelta(hours=hours))})
        with mock.patch.object(mcp_server, "_client", lambda: mock.Mock(data_dir=d, credentials={"token": "x"})):
            write(80)
            self.assertFalse(raw(goal="muscle").get("autoregulation"))
            write(1)
            self.assertEqual(raw(goal="muscle")["autoregulation"]["band"], "low")

    def test_caller_supplied_readiness_is_validated_on_every_path(self):
        import mcp_server
        c = gym_app.app.test_client()
        with mock.patch.object(features, "_client", mock.Mock(data_dir=fresh_dir(), credentials={"token": "x"})):
            for path, body in (("/api/coach/autoregulate", {"whoop_recovery": True}),
                               ("/api/coach/autoregulate", {"sleep_hours": 99}),
                               ("/api/coach/program", {"readiness": {"whoop_recovery": 500}})):
                resp = c.post(path, headers=BASIC, json=body)
                self.assertEqual((resp.status_code, resp.get_json()["error"]), (400, "invalid_readiness"), body)
        raw = getattr(mcp_server.generate_program, "fn", mcp_server.generate_program)
        with mock.patch.object(mcp_server, "_client", lambda: mock.Mock(data_dir=fresh_dir(), credentials={"token": "x"})):
            self.assertEqual(raw(readiness={"whoop_recovery": True})["error"], "request_failed")

    def test_valid_caller_readiness_still_autoregulates(self):
        c = gym_app.app.test_client()
        body = c.post("/api/coach/autoregulate", headers=BASIC, json={"whoop_recovery": 90}).get_json()
        self.assertIn("Whoop recovery 90%", json.dumps(body))


if __name__ == "__main__":
    unittest.main()
