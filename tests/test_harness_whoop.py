"""WHOOP parsers + token rotation, checked against WHOOP's own OpenAPI spec (tests/fixtures/whoop_openapi.json).

The synthetic payloads here are validated against the spec's `required` lists and enums, so a payload that the
spec would reject cannot make a parser test pass. Live behaviour is NOT covered: no WHOOP app exists yet.
"""
import json
import os
import sys
import tempfile
import threading
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
