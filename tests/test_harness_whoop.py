"""WHOOP parsers + token rotation, checked against WHOOP's own OpenAPI spec (tests/fixtures/whoop_openapi.json).

The synthetic payloads here are validated against the spec's `required` lists and enums, so a payload that the
spec would reject cannot make a parser test pass. Live behaviour is NOT covered: no WHOOP app exists yet.
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from harness import whoop
from harness.errors import SchemaDrift, SourceAuthError, SourceDown

SPEC = json.loads((Path(__file__).parent / "fixtures" / "whoop_openapi.json").read_text())["components"]["schemas"]
HOUR = 3_600_000


def conforms(schema_name: str, obj: dict) -> None:
    """Minimal OpenAPI check: required keys present, enum values legal, recursing into $ref'd objects."""
    schema = SPEC[schema_name]
    missing = [k for k in schema.get("required", []) if k not in obj]
    assert not missing, f"{schema_name} missing required {missing}"
    for key, prop in schema.get("properties", {}).items():
        if key not in obj or obj[key] is None:
            continue
        if "enum" in prop:
            assert obj[key] in prop["enum"], f"{schema_name}.{key}={obj[key]!r} not in {prop['enum']}"
        ref = prop.get("$ref")
        if ref and isinstance(obj[key], dict):
            conforms(ref.split("/")[-1], obj[key])


def recovery(state="SCORED", score=72.0, calibrating=False, updated="2026-10-26T14:05:00.000Z", cycle=1, created=None):
    rec = {"cycle_id": cycle, "sleep_id": "11111111-1111-1111-1111-111111111111", "user_id": 7,
           "created_at": created or updated, "updated_at": updated, "score_state": state}
    if state == "SCORED":
        rec["score"] = {"user_calibrating": calibrating, "recovery_score": score, "resting_heart_rate": 52.0,
                        "hrv_rmssd_milli": 65.0}
    conforms("Recovery", rec)
    return rec


def sleep(state="SCORED", light=4 * HOUR, sws=1.5 * HOUR, rem=1.5 * HOUR, nap=False, end="2026-10-26T14:00:00.000Z",
          updated=None):
    rec = {"id": "22222222-2222-2222-2222-222222222222", "cycle_id": 1, "user_id": 7, "created_at": end,
           "updated_at": updated or end, "start": "2026-10-26T06:00:00.000Z", "end": end,
           "timezone_offset": "-07:00", "nap": nap, "score_state": state}
    if state == "SCORED":
        rec["score"] = {"stage_summary": {"total_in_bed_time_milli": int(8 * HOUR), "total_awake_time_milli": int(0.5 * HOUR),
                                          "total_no_data_time_milli": 0, "total_light_sleep_time_milli": int(light),
                                          "total_slow_wave_sleep_time_milli": int(sws), "total_rem_sleep_time_milli": int(rem),
                                          "sleep_cycle_count": 4, "disturbance_count": 5},
                        "sleep_needed": {"baseline_milli": 8 * HOUR, "need_from_sleep_debt_milli": 0,
                                         "need_from_recent_strain_milli": 0, "need_from_recent_nap_milli": 0}}
        conforms("Sleep", rec)
        conforms("SleepStageSummary", rec["score"]["stage_summary"])
    return rec


def page(*records):
    return {"records": list(records), "next_token": None}


class Recovery(unittest.TestCase):
    def test_scored_recovery_becomes_a_reading(self):
        readings, notes = whoop.parse_recovery(page(recovery(score=72.0)))
        self.assertEqual(notes, [])
        self.assertEqual([(r.kind, r.value, r.source) for r in readings], [("whoop_recovery", 72.0, "whoop")])
        self.assertEqual(readings[0].as_of, datetime(2026, 10, 26, 14, 5, tzinfo=timezone.utc))

    def test_pending_and_unscorable_give_no_reading_and_a_reason(self):
        for state in ("PENDING_SCORE", "UNSCORABLE"):
            readings, notes = whoop.parse_recovery(page(recovery(state=state)))
            self.assertEqual(readings, [])
            self.assertIn(state, notes[0])

    def test_calibrating_user_gives_no_reading(self):
        readings, notes = whoop.parse_recovery(page(recovery(calibrating=True)))
        self.assertEqual(readings, [])
        self.assertIn("calibrating", notes[0])

    def test_newest_record_wins_even_if_an_older_one_was_scored(self):
        old = recovery(score=90.0, updated="2026-10-25T14:00:00Z", cycle=1)
        new = recovery(state="PENDING_SCORE", updated="2026-10-26T14:00:00Z", cycle=2)
        readings, notes = whoop.parse_recovery(page(old, new))
        self.assertEqual(readings, [], "must not fall back to yesterday's score")

    def test_rescoring_an_old_record_does_not_make_it_look_fresh(self):
        """updated_at moves when WHOOP rescores; as_of must stay at the measurement time (created_at)."""
        rescored = recovery(created="2026-10-23T14:00:00Z", updated="2026-10-26T14:00:00Z")
        readings, _ = whoop.parse_recovery(page(rescored))
        self.assertEqual(readings[0].as_of, datetime(2026, 10, 23, 14, 0, tzinfo=timezone.utc))

    def test_empty_page_is_a_note_not_an_error(self):
        self.assertEqual(whoop.parse_recovery(page()), ([], ["no recovery records returned"]))

    def test_drift_is_loud(self):
        for bad in (None, {}, {"records": "x"}, page({"score_state": "SCORED"}), page(recovery(score=140.0)),
                    page(recovery(score=True)), page({**recovery(), "created_at": "yesterday"}),
                    page({**recovery(), "created_at": "2026-10-26T14:00:00"})):
            with self.assertRaises(SchemaDrift, msg=repr(bad)[:80]):
                whoop.parse_recovery(bad)

    def test_scored_without_score_object_is_drift(self):
        rec = recovery()
        del rec["score"]
        with self.assertRaises(SchemaDrift):
            whoop.parse_recovery(page(rec))


class Sleep(unittest.TestCase):
    def test_hours_are_light_plus_sws_plus_rem_only(self):
        readings, _ = whoop.parse_sleep(page(sleep(light=4 * HOUR, sws=1.5 * HOUR, rem=1.5 * HOUR)))
        self.assertEqual([(r.kind, r.value) for r in readings], [("sleep_hours", 7.0)])  # awake + in-bed excluded

    def test_naps_are_ignored(self):
        night = sleep(end="2026-10-26T13:00:00Z")
        nap = sleep(nap=True, light=0.5 * HOUR, sws=0, rem=0, end="2026-10-26T20:00:00Z")
        readings, _ = whoop.parse_sleep(page(nap, night))
        self.assertEqual(readings[0].value, 7.0)

    def test_rescored_old_sleep_is_stamped_with_when_it_ended(self):
        rescored = sleep(end="2026-10-23T14:00:00Z", updated="2026-10-26T14:00:00Z")
        readings, _ = whoop.parse_sleep(page(rescored))
        self.assertEqual(readings[0].as_of, datetime(2026, 10, 23, 14, 0, tzinfo=timezone.utc))

    def test_a_night_split_over_two_records_counts_as_one_night(self):
        late = sleep(end="2026-10-26T14:00:00Z", light=1 * HOUR, sws=0, rem=0)
        early = sleep(end="2026-10-26T06:00:00Z", light=5 * HOUR, sws=1.5 * HOUR, rem=1.5 * HOUR)
        readings, _ = whoop.parse_sleep(page(late, early))
        self.assertEqual(readings[0].value, 9.0)
        self.assertEqual(readings[0].as_of, datetime(2026, 10, 26, 14, 0, tzinfo=timezone.utc))

    def test_a_different_cycles_sleep_is_not_added(self):
        today = sleep(end="2026-10-26T14:00:00Z")
        yesterday = {**sleep(end="2026-10-25T14:00:00Z"), "cycle_id": 0}
        readings, _ = whoop.parse_sleep(page(today, yesterday))
        self.assertEqual(readings[0].value, 7.0)

    def test_a_partly_scored_night_gives_no_reading(self):
        late = sleep(state="PENDING_SCORE", end="2026-10-26T14:00:00Z")
        early = sleep(end="2026-10-26T06:00:00Z")
        readings, notes = whoop.parse_sleep(page(late, early))
        self.assertEqual(readings, [])
        self.assertIn("PENDING_SCORE", notes[0])

    def test_wrong_typed_or_impossible_fields_are_schema_drift_never_a_raw_error(self):
        def with_stage(key, value):
            rec = sleep()
            rec["score"]["stage_summary"][key] = value
            return page(rec)

        bad_sleep = [with_stage("total_light_sleep_time_milli", None), with_stage("total_light_sleep_time_milli", "x"),
                     with_stage("total_light_sleep_time_milli", -5 * HOUR), with_stage("total_rem_sleep_time_milli", True),
                     page(5), page("x"), {"records": [5]}, page({**sleep(), "score": [1]}),
                     page(sleep(light=0, sws=0, rem=0))]  # a SCORED night with zero sleep is not real
        for bad in bad_sleep:
            with self.assertRaises(SchemaDrift, msg=repr(bad)[:100]):
                whoop.parse_sleep(bad)
        for bad in (page({**recovery(), "score": [1]}), page(5), {"records": [None]}, page({**recovery(), "score": "x"})):
            with self.assertRaises(SchemaDrift, msg=repr(bad)[:100]):
                whoop.parse_recovery(bad)

    def test_only_naps_means_no_reading(self):
        readings, notes = whoop.parse_sleep(page(sleep(nap=True)))
        self.assertEqual((readings, notes), ([], ["no non-nap sleep records returned"]))

    def test_unscored_sleep_gives_no_reading(self):
        readings, notes = whoop.parse_sleep(page(sleep(state="PENDING_SCORE")))
        self.assertEqual(readings, [])
        self.assertIn("PENDING_SCORE", notes[0])

    def test_drift_is_loud(self):
        no_stage = sleep()
        del no_stage["score"]["stage_summary"]["total_rem_sleep_time_milli"]
        for bad in (None, page({"score_state": "SCORED", "end": "2026-10-26T14:00:00Z"}), page(no_stage),
                    page(sleep(light=30 * HOUR))):
            with self.assertRaises(SchemaDrift, msg=repr(bad)[:80]):
                whoop.parse_sleep(bad)


class Tokens(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = whoop.TokenStore(Path(self.dir.name) / "whoop.token.json")
        self.store.save({"access_token": "a1", "refresh_token": "r1"})

    def tearDown(self):
        self.dir.cleanup()

    def test_refresh_persists_the_new_rotated_pair_with_private_mode(self):
        got = self.store.refresh_if_still_stale("r1", lambda r: {"access_token": "a2", "refresh_token": "r2"})
        self.assertEqual(got["refresh_token"], "r2")
        self.assertEqual(self.store.load()["refresh_token"], "r2")
        self.assertEqual(oct(self.store.path.stat().st_mode & 0o777), "0o600")

    def test_loser_of_a_race_reuses_the_winners_tokens(self):
        calls = []

        def refresh(old):
            calls.append(old)
            return {"access_token": "a2", "refresh_token": "r2"}

        self.store.refresh_if_still_stale("r1", refresh)
        again = self.store.refresh_if_still_stale("r1", refresh)  # second caller still holds the OLD r1
        self.assertEqual(calls, ["r1"], "refresh token must be spent exactly once")
        self.assertEqual(again["refresh_token"], "r2")

    def test_concurrent_refreshers_spend_the_token_once(self):
        calls, lock = [], threading.Lock()

        def refresh(old):
            with lock:
                calls.append(old)
            return {"access_token": "a2", "refresh_token": "r2"}

        threads = [threading.Thread(target=self.store.refresh_if_still_stale, args=("r1", refresh)) for _ in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len(calls), 1)

    def test_failed_refresh_leaves_old_tokens_and_leaks_nothing(self):
        def boom(old):
            raise ValueError("secret-r1-in-message")

        with self.assertRaises(SourceAuthError) as ctx:
            self.store.refresh_if_still_stale("r1", boom)
        self.assertNotIn("secret", str(ctx.exception))
        self.assertIsNone(ctx.exception.__cause__, "chaining would print the original message in tracebacks")
        self.assertIsNone(ctx.exception.__context__, "the original exception (and its text) must not be reachable")
        self.assertEqual(self.store.load()["refresh_token"], "r1")

    def test_traceback_never_prints_the_callers_in_flight_exception(self):
        """The 401 that triggers a refresh may carry token text; it must not be printed via exception chaining."""
        import traceback

        def boom(old):
            raise ValueError("refresh-body-secret")

        try:
            try:
                raise ValueError("outer-401-secret-token")
            except ValueError:
                self.store.refresh_if_still_stale("r1", boom)
        except SourceAuthError as err:
            text = "".join(traceback.format_exception(type(err), err, err.__traceback__))
        self.assertNotIn("secret", text)
        self.assertNotIn("During handling of the above exception", text)
        self.assertNotIn("direct cause", text)

    def test_separate_processes_spend_the_refresh_token_once(self):
        """Real OS-level contention: 5 interpreters race to refresh the same stale token."""
        import subprocess
        calls = Path(self.dir.name) / "refresh_calls.log"
        script = (
            "import sys, time\n"
            f"sys.path.insert(0, {str(Path(__file__).parent.parent)!r})\n"
            "from harness import whoop\n"
            f"store = whoop.TokenStore({str(self.store.path)!r})\n"
            f"import os, pathlib\n"
            f"d = pathlib.Path({self.dir.name!r})\n"
            "(d / f'ready.{os.getpid()}').touch()\n"
            "while not (d / 'go').exists():\n"
            "    time.sleep(0.005)\n"
            "def refresh(old):\n"
            f"    open({str(calls)!r}, 'a').write(old + '\\n')\n"
            "    time.sleep(0.3)\n"
            "    return {'access_token': 'a2', 'refresh_token': 'r2'}\n"
            "print(store.refresh_if_still_stale('r1', refresh)['refresh_token'])\n"
        )
        procs = []
        try:
            for _ in range(5):  # appended one by one INSIDE the try so a failed launch still reaps the earlier children
                procs.append(subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE,
                                              stderr=subprocess.PIPE, text=True))
            deadline = time.monotonic() + 30
            while len(list(Path(self.dir.name).glob("ready.*"))) < len(procs):  # barrier: start the race together
                self.assertLess(time.monotonic(), deadline, "child processes never became ready")
                time.sleep(0.01)
            (Path(self.dir.name) / "go").touch()
            results = [p.communicate(timeout=60) + (p.returncode,) for p in procs]
        finally:
            for child in procs:  # never leave barrier-waiting children behind if a sibling died before 'ready'
                if child.poll() is None:
                    child.kill()
                child.communicate()
        for out, err, code in results:
            self.assertEqual((code, out.strip()), (0, "r2"), err)
        self.assertEqual(calls.read_text().split(), ["r1"], "the stale refresh token must be spent exactly once")
        self.assertEqual(self.store.load()["refresh_token"], "r2")

    def test_a_transient_write_failure_is_retried_into_the_main_file(self):
        from unittest import mock
        real = whoop.localsecrets.write_json_private
        attempts = []

        def once_flaky(path, data):
            attempts.append(Path(path).name)
            if len(attempts) == 1:
                raise OSError("EINTR-ish")
            return real(path, data)

        with mock.patch.object(whoop.localsecrets, "write_json_private", side_effect=once_flaky):
            self.store.save({"access_token": "a2", "refresh_token": "r2"})
        self.assertEqual(attempts, ["whoop.token.json", "whoop.token.json"])
        self.assertEqual(self.store.load()["refresh_token"], "r2")

    def test_public_save_takes_the_lock(self):
        import fcntl
        from unittest import mock
        holder = os.open(self.store.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(holder, fcntl.LOCK_EX)
        try:
            with mock.patch.object(whoop, "LOCK_TIMEOUT_S", 0.2), self.assertRaises(SourceDown) as ctx:
                self.store.save({"access_token": "a2", "refresh_token": "r2"})
            self.assertIn("lock busy", str(ctx.exception))
        finally:
            os.close(holder)
        self.assertEqual(self.store.load()["refresh_token"], "r1", "nothing may be written without the lock")

    def test_malformed_refresh_responses_become_sanitised_errors(self):
        import traceback
        for bad in (None, ["not", "a", "dict"], {"access_token": "a", "refresh_token": "r", "x": object()},
                    {"access_token": 123, "refresh_token": "r"}, {"access_token": "a", "refresh_token": {"t": "private-secret"}},
                    {"access_token": "a", "refresh_token": "x" * 5000}):
            try:
                try:
                    raise ValueError("outer-401-secret-token")
                except ValueError:
                    self.store.refresh_if_still_stale("r1", lambda old, bad=bad: bad)
            except (SourceDown, SourceAuthError) as err:
                text = "".join(traceback.format_exception(type(err), err, err.__traceback__))
                if not isinstance(bad, dict):
                    self.assertIn("not a JSON object", str(err), repr(bad))
            else:
                self.fail(f"no error for {bad!r}")
            self.assertNotIn("secret", text, repr(bad))
            self.assertEqual(self.store.load()["refresh_token"], "r1")

    def test_save_called_inside_an_except_block_does_not_print_the_callers_exception(self):
        import traceback
        for bad in ({"access_token": "a", "refresh_token": ""}, ["x"]):
            try:
                try:
                    raise RuntimeError("401 refresh_token=OLDSECRET")
                except RuntimeError:
                    self.store.save(bad)
            except SourceDown as err:
                text = "".join(traceback.format_exception(type(err), err, err.__traceback__))
            self.assertNotIn("OLDSECRET", text)

    def test_a_corrupt_or_foreign_token_file_is_a_source_down_and_refresh_recovers(self):
        for content in ("", "[]", "not json", '"str"'):
            self.store.path.write_text(content)
            os.chmod(self.store.path, 0o600)
            with self.assertRaises(SourceDown, msg=content):
                self.store.load()
            got = self.store.refresh_if_still_stale("r1", lambda old: {"access_token": "a2", "refresh_token": "r2"})
            self.assertEqual((content, got["refresh_token"]), (content, "r2"))
            self.assertEqual(self.store.load()["refresh_token"], "r2")

    def test_a_world_readable_token_file_is_refused_not_a_raw_permission_error(self):
        os.chmod(self.store.path, 0o644)
        with self.assertRaises(SourceDown):
            self.store.load()

    def test_a_stored_dict_without_a_refresh_token_is_not_treated_as_already_rotated(self):
        self.store.path.write_text(json.dumps({"access_token": "x"}))
        os.chmod(self.store.path, 0o600)
        got = self.store.refresh_if_still_stale("r1", lambda old: {"access_token": "a2", "refresh_token": "r2"})
        self.assertEqual(got["refresh_token"], "r2")

    def test_token_files_are_fsynced_in_the_right_order(self):
        """data flushed -> atomic rename -> directory entry flushed. Order matters for crash safety."""
        from unittest import mock
        events = []
        real_fsync, real_replace = os.fsync, os.replace

        def fsync(fd):
            kind = "dir" if os.path.isdir(f"/dev/fd/{fd}") else "file"
            events.append(f"fsync-{kind}")
            return real_fsync(fd)

        def replace(src, dst):
            events.append("replace")
            return real_replace(src, dst)

        with mock.patch.object(os, "fsync", side_effect=fsync), mock.patch.object(os, "replace", side_effect=replace):
            self.store.save({"access_token": "a2", "refresh_token": "r2"})
        self.assertEqual(events, ["fsync-file", "replace", "fsync-dir"])

    def test_every_failure_raised_by_whoop_py_suppresses_exception_chaining(self):
        """Structural guard for the whole class: a raise of our error types without `from None` can print the
        caller's in-flight exception (often a 401 that contains a token) in a traceback."""
        import ast
        tree = ast.parse((Path(__file__).parent.parent / "harness" / "whoop.py").read_text())
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call) \
                    and getattr(node.exc.func, "id", None) in ("SourceDown", "SourceAuthError", "SchemaDrift"):
                # SchemaDrift is raised by the pure parsers (no token handling, no caller exception in flight)
                if node.exc.func.id != "SchemaDrift" and not (isinstance(node.cause, ast.Constant) and node.cause.value is None):
                    offenders.append(node.lineno)
        self.assertEqual(offenders, [], f"raise without `from None` at whoop.py lines {offenders}")

    def test_load_inside_an_except_block_does_not_print_the_callers_exception(self):
        import traceback
        for content in ("[]", "not json", ""):
            self.store.path.write_text(content)
            os.chmod(self.store.path, 0o600)
            try:
                try:
                    raise RuntimeError("401 refresh_token=OLDSECRET")
                except RuntimeError:
                    self.store.load()
            except SourceDown as err:
                text = "".join(traceback.format_exception(type(err), err, err.__traceback__))
            self.assertNotIn("OLDSECRET", text, repr(content))

    def test_there_is_exactly_one_token_file(self):
        """No side files except the lock: nothing to order, nothing to shadow."""
        self.store.save({"access_token": "a2", "refresh_token": "r2"})
        self.assertEqual(sorted(p.name for p in Path(self.dir.name).iterdir()), ["whoop.token.json", "whoop.token.json.lock"])

    def test_unsavable_tokens_raise_source_down_not_an_auth_error(self):
        from unittest import mock
        with mock.patch.object(whoop.localsecrets, "write_json_private", side_effect=OSError("read-only fs")):
            with self.assertRaises(SourceDown) as ctx:
                self.store.refresh_if_still_stale("r1", lambda old: {"access_token": "a2", "refresh_token": "r2"})
        self.assertNotIsInstance(ctx.exception, SourceAuthError)
        self.assertIn("re-authorised", str(ctx.exception))
        self.assertNotIn("r2", str(ctx.exception).replace("re-authorised", ""))

    def test_source_down_paths_also_hide_the_callers_exception(self):
        import traceback
        from harness.errors import SourceDown as Down

        def down(old):
            raise Down("whoop", "503")

        for refresh in (down, lambda old: {"access_token": "a2"}):  # SourceDown from refresh_fn, and from save()
            try:
                try:
                    raise ValueError("outer-401-secret-token")
                except ValueError:
                    self.store.refresh_if_still_stale("r1", refresh)
            except Down as err:
                text = "".join(traceback.format_exception(type(err), err, err.__traceback__))
            self.assertNotIn("secret", text)
            self.assertNotIn("During handling of the above exception", text)
            self.assertEqual(self.store.load()["refresh_token"], "r1")

    def test_a_hung_refresher_cannot_block_everyone_forever(self):
        import fcntl
        self.store.path.parent.mkdir(parents=True, exist_ok=True)
        holder = os.open(self.store.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(holder, fcntl.LOCK_EX)
        waiter = os.open(self.store.lock_path, os.O_RDWR)
        try:
            with self.assertRaises(SourceDown) as ctx:
                whoop.TokenStore._lock(waiter, timeout_s=0.2)
            self.assertIn("lock busy", str(ctx.exception))
        finally:
            os.close(waiter)
            os.close(holder)

    def test_source_down_passes_through_untouched(self):
        def down(old):
            raise SourceDown("whoop", "503")

        with self.assertRaises(SourceDown):
            self.store.refresh_if_still_stale("r1", down)

    def test_response_missing_a_token_is_rejected_and_not_saved(self):
        with self.assertRaises(SourceDown):
            self.store.refresh_if_still_stale("r1", lambda r: {"access_token": "a2"})
        self.assertEqual(self.store.load()["refresh_token"], "r1")


class SpecFixture(unittest.TestCase):
    def test_parsers_use_field_names_the_spec_actually_defines(self):
        stage = SPEC["SleepStageSummary"]["properties"]
        for key in ("total_light_sleep_time_milli", "total_slow_wave_sleep_time_milli", "total_rem_sleep_time_milli"):
            self.assertIn(key, stage)
        self.assertIn("recovery_score", SPEC["RecoveryScore"]["properties"])
        self.assertEqual(SPEC["Recovery"]["properties"]["score_state"]["enum"], ["SCORED", "PENDING_SCORE", "UNSCORABLE"])


if __name__ == "__main__":
    unittest.main()
