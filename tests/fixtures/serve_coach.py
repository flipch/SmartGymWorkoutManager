"""A throwaway fixture server for looking at the /coach page in a browser. NOT part of the deployed app, NOT run by the tests.

    .venv/bin/python tests/fixtures/serve_coach.py [--port 5099] [--writes] [--data-dir DIR]

It imports the real app, with a THROWAWAY data dir, a dummy 32-character GYM_API_TOKEN (any username, that token as the Basic-auth
password), a fake vendor client (no Speediance call), and a fake WHOOP HTTP layer (no WHOOP call). No credentials, no network.
Control routes under /__fx/ (same Basic auth) put the page in each state; see CONTROL below.
"""
import argparse
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
DUMMY_TOKEN = "fixture-token-" + "x" * 18  # 32 characters, not a secret

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, default=5099)
parser.add_argument("--writes", action="store_true", help="start with GYM_ALLOW_WRITES=1")
parser.add_argument("--data-dir", default=None)
args = parser.parse_args()

DATA_DIR = args.data_dir or tempfile.mkdtemp(prefix="gym-fixture-")
os.environ.update({"GYM_API_TOKEN": DUMMY_TOKEN, "DATA_DIR": DATA_DIR, "TZ": "UTC",
                   "WHOOP_CLIENT_ID": "00000000-0000-4000-8000-000000000000", "WHOOP_CLIENT_SECRET": "d" * 64,
                   "WHOOP_REDIRECT_URI": f"http://localhost:{args.port}/whoop/callback"})
os.environ.pop("GYM_AUTH_DISABLED", None)
if args.writes:
    os.environ["GYM_ALLOW_WRITES"] = "1"
else:
    os.environ.pop("GYM_ALLOW_WRITES", None)
sys.path.insert(0, ROOT)

from flask import Blueprint, jsonify, request  # noqa: E402

import app as gym_app  # noqa: E402
import features  # noqa: E402
import state_io  # noqa: E402
import whoop_sync  # noqa: E402

LIB = [  # a pretend exercise library: "Face pull" and the calf raise are missing, "Cable crunch" matches only loosely
    {"id": i, "title": t, "mainMuscleGroupName": m, "isLeftRight": 0} for i, (t, m) in enumerate([
        ("Cable Chest Press", "Chest"), ("Incline Cable Press", "Chest"), ("Cable Fly", "Chest"), ("Lat Pulldown", "Back"),
        ("Seated Cable Row", "Back"), ("Cable Overhead Press", "Shoulders"), ("Cable Lateral Raise", "Shoulders"),
        ("Cable Front Squat", "Quads"), ("Leg Extension", "Quads"), ("Romanian Deadlift", "Hamstrings"), ("Leg Curl", "Hamstrings"),
        ("Cable Curl", "Biceps"), ("Triceps Pushdown", "Triceps"), ("Hip Thrust", "Glutes"), ("Crunch Machine", "Abs"),
        ("<img src=x onerror=alert(1)> Hostile Press", "Chest")], start=1)]


class FakeClient:
    """Stands in for SpeedianceClient: no network, no real account."""

    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.credentials = {"token": "fixture", "user_id": "1", "unit": 0, "unit_confirmed": True}
        self.saved = []

    def get_library(self, strict=False):
        return LIB

    def prepare_workout(self, name, exercises):
        return None

    def save_workout(self, name, exercises):
        self.saved.append((name, exercises))
        return {"code": 0, "data": {"id": 1000 + len(self.saved), "code": f"FX{len(self.saved)}"}}


client = FakeClient(DATA_DIR)
features._client = client
gym_app.client = client


class FakeWhoopHttp:
    """The WHOOP HTTP layer. mode: ok | down | dead. Scripted from the control route; records nothing and calls nothing real."""
    mode = "ok"
    recovery = 42
    sleep = 6.4

    class Resp:
        def __init__(self, status, body):
            self.status_code, self._b, self.headers = status, body, {}

        def json(self):
            return self._b

    @staticmethod
    def _iso(h):
        return (datetime.now(timezone.utc) - timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    def get(self, url, **kw):
        R = self.Resp
        if self.mode == "down":
            return R(503, {})
        if self.mode == "dead" and kw["headers"]["Authorization"] == "Bearer fixture-access":
            return R(401, {})
        if "recovery" in url:
            return R(200, {"records": [{"cycle_id": 1, "created_at": self._iso(1.5), "score_state": "SCORED",
                                        "score": {"user_calibrating": False, "recovery_score": self.recovery}}]})
        ms = int(self.sleep * 3_600_000)
        return R(200, {"records": [{"cycle_id": 1, "nap": False, "end": self._iso(2), "score_state": "SCORED",
                                    "score": {"stage_summary": {"total_light_sleep_time_milli": ms // 2,
                                                                "total_slow_wave_sleep_time_milli": ms // 4,
                                                                "total_rem_sleep_time_milli": ms - ms // 2 - ms // 4}}}]})

    def post(self, url, **kw):
        if self.mode == "dead":
            return self.Resp(400, {"error": "invalid_grant"})
        return self.Resp(200, {"access_token": "fixture-access-2", "refresh_token": "fixture-refresh-2", "expires_in": 3600,
                               "scope": "offline read:recovery read:sleep", "token_type": "bearer"})


whoop_sync.requests = FakeWhoopHttp()

fx = Blueprint("fx", __name__, url_prefix="/__fx")
CONTROL = """GET /__fx/readiness?whoop=42&whoop_age=1&whoop_src=whoop&sleep=6.5&sleep_age=1&sleep_src=whoop&subj=3&subj_age=1  (omit a field to leave it out; clear=1 empties)
GET /__fx/whoop?state=none|connected|failing|reconsent&mode=ok|down|dead&recovery=42
GET /__fx/writes?on=1|0     GET /__fx/unit?confirmed=1|0     GET /__fx/avoid?name=...&pain=1     GET /__fx/reset"""


@fx.route("/readiness")
def fx_readiness():
    q = request.args
    path = os.path.join(DATA_DIR, "readiness.json")
    if q.get("clear"):
        if os.path.exists(path):
            os.unlink(path)
        return jsonify({"ok": True})
    now = datetime.now(timezone.utc)
    data = {"updated": state_io.utc_stamp(now), "field_updated": {}, "field_source": {}}
    for field, key, cast in (("whoop_recovery", "whoop", float), ("sleep_hours", "sleep", float), ("subjective", "subj", float)):
        if q.get(key) is not None:
            data[field] = cast(q[key])
            data["field_updated"][field] = state_io.utc_stamp(now - timedelta(hours=float(q.get(key + "_age", 1))))
            if q.get(key + "_src"):
                data["field_source"][field] = q[key + "_src"]
    if q.get("hrv"):
        data["hrv_vs_baseline"] = q["hrv"]
        data["field_updated"]["hrv_vs_baseline"] = state_io.utc_stamp(now - timedelta(hours=1))
    if not data["field_source"]:
        data.pop("field_source")
    if q.get("legacy"):  # an older file: no field stamps, no sources
        data.pop("field_updated", None)
        data.pop("field_source", None)
    state_io.atomic_write_json(path, data)
    return jsonify({"ok": True})


@fx.route("/whoop")
def fx_whoop():
    q = request.args
    state = q.get("state", "connected")
    FakeWhoopHttp.mode = q.get("mode", "ok")
    FakeWhoopHttp.recovery = float(q.get("recovery", 42))
    token = os.path.join(DATA_DIR, "whoop.token.json")
    for name in ("whoop_state.json", "whoop_pull.lock.x"):
        if os.path.exists(os.path.join(DATA_DIR, name)):
            os.unlink(os.path.join(DATA_DIR, name))
    if state == "none":
        if os.path.exists(token):
            os.unlink(token)
    else:
        from harness.whoop import TokenStore
        TokenStore(token).save({"access_token": "fixture-access", "refresh_token": "fixture-refresh", "expires_in": 3600,
                                "scope": "offline read:recovery read:sleep", "token_type": "bearer"})
    now = datetime.now(timezone.utc)
    if state == "failing":
        state_io.atomic_write_json(os.path.join(DATA_DIR, "whoop_state.json"), {
            "last_success": state_io.utc_stamp(now - timedelta(hours=3)), "last_attempt": state_io.utc_stamp(now - timedelta(minutes=20)),
            "next_allowed": state_io.utc_stamp(now + timedelta(hours=1)), "failures": 3,
            "error": {"kind": "unavailable", "message": "WHOOP unavailable: whoop: /v2/recovery failed (HTTP 503)"}})
    elif state == "reconsent":
        state_io.atomic_write_json(os.path.join(DATA_DIR, "whoop_state.json"), {
            "last_success": state_io.utc_stamp(now - timedelta(days=2)), "last_attempt": state_io.utc_stamp(now - timedelta(hours=1)),
            "needs_reconsent": True, "failures": 1, "error": {"kind": "needs_reconsent", "message": "x"}})
    elif state == "connected":
        state_io.atomic_write_json(os.path.join(DATA_DIR, "whoop_state.json"), {
            "last_success": state_io.utc_stamp(now - timedelta(minutes=5)), "last_attempt": state_io.utc_stamp(now - timedelta(minutes=5)),
            "next_allowed": state_io.utc_stamp(now + timedelta(minutes=10)), "failures": 0, "error": None, "notes": []})
    return jsonify({"ok": True, "state": state})


@fx.route("/writes")
def fx_writes():
    if request.args.get("on") == "1":
        os.environ["GYM_ALLOW_WRITES"] = "1"
    else:
        os.environ.pop("GYM_ALLOW_WRITES", None)
    return jsonify({"writes": os.environ.get("GYM_ALLOW_WRITES") == "1"})


@fx.route("/unit")
def fx_unit():
    client.credentials["unit_confirmed"] = request.args.get("confirmed") == "1"
    return jsonify({"unit_confirmed": client.credentials["unit_confirmed"]})


@fx.route("/avoid")
def fx_avoid():
    name = request.args["name"]
    features.log_feedback({"pain": True, "exercise": name} if request.args.get("pain") else {"avoid": name}, DATA_DIR)
    return jsonify({"ok": True})


@fx.route("/reset")
def fx_reset():
    for name in os.listdir(DATA_DIR):
        if name.startswith(("readiness", "coach_", "whoop")):
            os.unlink(os.path.join(DATA_DIR, name))
    client.saved.clear()
    return jsonify({"ok": True})


@fx.route("/saved")
def fx_saved():
    return jsonify({"saved": [n for n, _ in client.saved]})


gym_app.app.register_blueprint(fx)

if __name__ == "__main__":
    print(f"fixture server on http://127.0.0.1:{args.port}/coach  data dir {DATA_DIR}  writes {'on' if args.writes else 'off'}", flush=True)
    print(CONTROL, flush=True)
    gym_app.app.run(host="127.0.0.1", port=args.port, threaded=True, use_reloader=False)
