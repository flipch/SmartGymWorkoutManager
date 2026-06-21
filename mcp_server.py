"""
mcp_server.py — Model Context Protocol server for SmartGymWorkoutManager
(flipch fork addition).

Launched alongside the Flask web UI (see docker-compose.yml). Exposes the
Speediance "Smart Gym" account of the *currently logged-in user* to AI agents
so they can read and manage workouts, the schedule, and training history.

Transport: streamable-http on :5002  (endpoint: http://<host>:5002/mcp)

"Given user" = whoever is authenticated in the shared config.json (written by
the web UI at /login). The client is reloaded from config.json on every call,
so logging in / out through the UI is reflected immediately — no restart.
"""
import os
from datetime import date, timedelta

from mcp.server.fastmcp import FastMCP

import coach
from api_client import SpeedianceClient
from features import (  # shared so MCP == HTTP logic
    _session_dates, _coach_workout_to_exercises,
    _detailed_records, fetch_session_detail, _summarize_session, _unit_label,
    _feedback_profile, _load_readiness, apply_program,
    log_feedback as _persist_feedback, save_readiness as _persist_readiness,
)

mcp = FastMCP(
    "smart-gym",
    host=os.environ.get("MCP_HOST", "0.0.0.0"),
    port=int(os.environ.get("MCP_PORT", "5002")),
)


def _client():
    """Fresh client each call → always reads the latest config.json token."""
    return SpeedianceClient()


def _require_auth(c):
    if not c.credentials.get("token"):
        raise PermissionError(
            "Not authenticated. Log in to the Smart Gym web UI (/settings) "
            "with your Speediance account first; the MCP shares that session."
        )


def _safe(fn):
    """Run a client call, normalising errors into a JSON-friendly dict."""
    try:
        return fn()
    except PermissionError as e:
        return {"error": "not_authenticated", "message": str(e)}
    except Exception as e:  # noqa: BLE001 — surface anything as a clean payload
        msg = str(e)
        if msg == "Unauthorized":
            return {"error": "unauthorized",
                    "message": "Speediance rejected the token. Re-login via /settings."}
        return {"error": "request_failed", "message": msg}


# ─────────────────────────────────────────────── account / status
@mcp.tool()
def whoami() -> dict:
    """Return whether a Speediance user is logged in, plus their region/units.
    Use this first to confirm the MCP has an authenticated session."""
    c = _client()
    return {
        "authenticated": bool(c.credentials.get("token")),
        "user_id": c.credentials.get("user_id") or None,
        "region": c.credentials.get("region", "Global"),
        "unit": "imperial" if c.credentials.get("unit") else "metric",
        "device_type": c.credentials.get("device_type", 1),
    }


# ─────────────────────────────────────────────── workouts (CRUD)
@mcp.tool()
def list_workouts() -> list:
    """List the user's saved custom workout templates (id, name, code).
    These codes are what schedule_workout and get_workout accept."""
    def run():
        c = _client(); _require_auth(c)
        return c.get_user_workouts()
    return _safe(run)


@mcp.tool()
def get_workout(code: str) -> dict:
    """Get the full detail of a single workout template by its `code`
    (exercises, sets, reps, weights)."""
    def run():
        c = _client(); _require_auth(c)
        return c.get_workout_detail(code) or {"error": "not_found", "code": code}
    return _safe(run)


@mcp.tool()
def delete_workout(template_id: str) -> dict:
    """Permanently delete a custom workout template by its id (Speediance ids can be long
    hex strings, so this is a string). Destructive — confirm the id with list_workouts first."""
    def run():
        c = _client(); _require_auth(c)
        c.delete_workout(template_id)
        return {"deleted": template_id}
    return _safe(run)


@mcp.tool()
def save_workout(name: str, exercises: list, template_id: int = None) -> dict:
    """Create (template_id omitted) or update (template_id given) a workout.

    `exercises` is a list of dicts, each:
      {"groupId": <int exercise id from search_exercises>,
       "sets": [{"reps": 10, "weight": 20.0, "rest": 60, "unit": "reps"|"sec"}, ...]}
    Returns the Speediance API response."""
    def run():
        c = _client(); _require_auth(c)
        return c.save_workout(name, exercises, template_id)
    return _safe(run)


# ─────────────────────────────────────────────── schedule / calendar
@mcp.tool()
def get_calendar(month: str = None) -> list:
    """Get scheduled workouts/courses for a month ('YYYY-MM', default current)."""
    def run():
        c = _client(); _require_auth(c)
        m = month or date.today().strftime("%Y-%m")
        return c.get_calendar_month(m)
    return _safe(run)


@mcp.tool()
def schedule_workout(day: str, template_code: str, add: bool = True) -> dict:
    """Add (add=True) or remove (add=False) a workout from the calendar.
    `day` is 'YYYY-MM-DD'; `template_code` comes from list_workouts."""
    def run():
        c = _client(); _require_auth(c)
        ok = c.schedule_workout(day, template_code, 1 if add else 0)
        return {"day": day, "template_code": template_code,
                "action": "scheduled" if add else "unscheduled", "ok": bool(ok)}
    return _safe(run)


# ─────────────────────────────────────────────── history / sessions
@mcp.tool()
def training_history(days: int = 30) -> dict:
    """List completed training sessions in the last `days` days."""
    def run():
        c = _client(); _require_auth(c)
        end = date.today(); start = end - timedelta(days=max(1, days))
        recs = c.get_training_history(start.isoformat(), end.isoformat())
        return {"start": start.isoformat(), "end": end.isoformat(),
                "count": len(recs) if isinstance(recs, list) else None,
                "sessions": recs}
    return _safe(run)


@mcp.tool()
def last_workout(n: int = 1, days: int = 60) -> dict:
    """Full per-set detail of a recent COMPLETED workout (n=1 = most recent, n=2 = the one
    before, …) — the same data as the web History detail view. Each exercise lists its sets
    with reps done/target, the actual weight used (including any mid-set weight changes),
    per-set volume, L/R side for unilateral work, and the rating; plus session totals
    (duration, calories, volume). Use this to review or critique what the user actually did."""
    def run():
        c = _client(); _require_auth(c)
        recs = _detailed_records(c, days)
        if len(recs) < n:
            return {"error": "not_found",
                    "message": f"Fewer than {n} detailed workouts in the last {days} days."}
        return fetch_session_detail(c, recs[n - 1])
    return _safe(run)


@mcp.tool()
def recent_workouts(days: int = 30) -> dict:
    """List recent completed workouts (name, date, duration, calories, volume, training_id)
    newest first — a quick index. Use last_workout or workout_session for full per-set detail."""
    def run():
        c = _client(); _require_auth(c)
        recs = _detailed_records(c, days)
        return {"count": len(recs), "workouts": [{
            "training_id": r.get("trainingId"), "name": r.get("title") or "Workout",
            "type": r.get("type"), "start_ts": r.get("startTimestamp"),
            "duration_sec": r.get("trainingTime"),
            "calories": round(r.get("calorie") or 0),
            "volume": round(r.get("totalCapacity") or 0),
        } for r in recs]}
    return _safe(run)


@mcp.tool()
def workout_session(training_id: int, training_type: str = "custom") -> dict:
    """Full per-set detail for a specific completed session by training_id (from
    recent_workouts). training_type: 'custom' or 'course'."""
    def run():
        c = _client(); _require_auth(c)
        detail = c.get_training_detail(training_id, training_type)
        record = {"trainingId": training_id, "title": None,
                  "type": 2 if training_type == "course" else 5}
        return _summarize_session(record, detail, _unit_label(c))
    return _safe(run)


@mcp.tool()
def training_stats(days: int = 30) -> dict:
    """Aggregated training stats (volume, calories, etc.) over the last `days`."""
    def run():
        c = _client(); _require_auth(c)
        end = date.today(); start = end - timedelta(days=max(1, days))
        return c.get_training_stats(start.isoformat(), end.isoformat())
    return _safe(run)


# ─────────────────────────────────────────────── insights & coaching
@mcp.tool()
def workout_insights() -> dict:
    """Computed training summary over the last 90 days: streak, weekly frequency,
    last session. Mirrors the web /api/insights dashboard."""
    def run():
        c = _client(); _require_auth(c)
        end = date.today(); start = end - timedelta(days=90)
        recs = c.get_training_history(start.isoformat(), end.isoformat())
        recs = recs if isinstance(recs, list) else []
        dates = _session_dates(recs)
        today = date.today()
        streak = 0
        if dates:
            dset = set(dates)
            cur = today if today in dset else today - timedelta(days=1)
            while cur in dset:
                streak += 1
                cur -= timedelta(days=1)
        last30 = sum(1 for d in dates if d >= today - timedelta(days=30))
        return {
            "total_sessions": len(recs),
            "active_days": len(dates),
            "current_streak_days": streak,
            "sessions_last_7d": sum(1 for d in dates if d >= today - timedelta(days=7)),
            "sessions_last_30d": last30,
            "avg_sessions_per_week": round(last30 / (30 / 7), 1) if last30 else 0,
            "last_session": dates[-1].isoformat() if dates else None,
        }
    return _safe(run)


@mcp.tool()
def recovery_recommendation() -> dict:
    """Should the user train, rest, or do active recovery today? Based on recent
    training frequency. Mirrors the web /api/recovery endpoint."""
    def run():
        c = _client(); _require_auth(c)
        end = date.today(); start = end - timedelta(days=21)
        recs = c.get_training_history(start.isoformat(), end.isoformat())
        dates = _session_dates(recs if isinstance(recs, list) else [])
        today = date.today()
        last7 = sum(1 for d in dates if d >= today - timedelta(days=7))
        days_since = (today - dates[-1]).days if dates else None
        if days_since is None or days_since >= 3:
            rec, msg = "train", "Well rested — a great day to train."
        elif last7 >= 6:
            rec, msg = "rest", "6+ sessions in 7 days. Take a rest/active-recovery day."
        elif days_since == 0 and last7 >= 2:
            rec, msg = "active_recovery", "Already trained today — keep it light."
        else:
            rec, msg = "train", "Good to train today."
        return {"recommendation": rec, "message": msg,
                "days_since_last": days_since, "sessions_last_7d": last7}
    return _safe(run)


# ─────────────────────────────────────────────── library (for building plans)
@mcp.tool()
def search_exercises(query: str = "", limit: int = 25) -> list:
    """Search the exercise library by name (case-insensitive). Returns matching
    exercises with their groupId — feed those ids into save_workout."""
    def run():
        c = _client(); _require_auth(c)
        lib = c.get_library() or []
        q = (query or "").strip().lower()
        out = []
        for ex in lib:
            if not isinstance(ex, dict):
                continue
            # the library stores the display name under `title` (name is null)
            name = str(ex.get("title") or ex.get("name") or ex.get("actionName") or "")
            if q and q not in name.lower():
                continue
            out.append({
                "groupId": ex.get("id") or ex.get("groupId"),
                "name": name,
                "muscle": ex.get("mainMuscleGroupName"),
                "category": ex.get("category_name"),
                "device_type": ex.get("device_type"),
            })
            if len(out) >= max(1, limit):
                break
        return out
    return _safe(run)


# ─────────────────────────────────────────────── AI Coach (evidence-based engine)
@mcp.tool()
def coaching_principles(topic: str = "") -> dict:
    """Return the engine's evidence-based training principles (cite knowledge/*.md).
    Optional topic: volume, intensity, rir, frequency, rest, balance, modes, ordering,
    recovery, fatloss. Use this to ground your coaching in the science."""
    return coach.principles(topic or None)


@mcp.tool()
def generate_program(goal: str = "general", days_per_week: int = 4,
                     experience: str = "intermediate", readiness: dict = None) -> dict:
    """Generate a full weekly Speediance program tuned to the user.
    goal: muscle | strength | fatloss | general. days_per_week: 1-5.
    experience: beginner | intermediate | advanced.
    readiness (optional): {whoop_recovery 0-100, hrv_vs_baseline above|within|below,
      rhr_delta_bpm, sleep_hours, subjective 1-5, acwr} -> autoregulates volume/intensity/RIR.
    Returns days with ordered exercises (sets/reps/%1RM/RIR/rest), the Speediance MODE per
    exercise (standard/chain/eccentric/constant/spotter), setup-swap count, and a quality
    review (volume vs landmarks, push/pull balance, eccentric share)."""
    def run():
        # works offline as a PT; auto-applies any persisted readiness + feedback so the
        # program adapts to what the user/agent has reported (real-time, stateful loop).
        c = _client()
        r = readiness
        if r is None:
            stored = _load_readiness(c.data_dir)
            if any(k in stored for k in ("whoop_recovery", "hrv_vs_baseline",
                                         "rhr_delta_bpm", "sleep_hours", "subjective")):
                r = stored
        prof = _feedback_profile(c.data_dir)
        fb = prof if prof.get("events") else None
        return coach.generate_program(goal=goal, days_per_week=days_per_week,
                                      experience=experience, readiness=r, feedback=fb)
    return _safe(run)


@mcp.tool()
def create_workout_from_program(program: dict, day_index: int = None,
                                name: str = None, dry_run: bool = False) -> dict:
    """Import a generated program (the `generate_program` result) into REAL Speediance workout
    templates. Resolves each exercise name to a library groupId, builds the sets payload, and
    saves it (set dry_run=true to preview the resolution without saving). day_index saves a
    single day; omit it to save every day as its own template. Unresolved exercises are
    reported, never silently dropped. This is the one-call 'generate then save to my gym' path."""
    def run():
        c = _client(); _require_auth(c)
        if not isinstance(program, dict) or not program.get("days"):
            return {"error": "no_program", "message": "Pass a generate_program result as `program`."}
        return apply_program(c, program, day_index, name, dry_run)
    return _safe(run)


@mcp.tool()
def critique_exercises(exercises: list, goal: str = "general", mode: str = "session") -> dict:
    """Critique a workout for safety/balance. `exercises`: list of {name, sets, reps, mode?}
    (real Speediance names work — they're classified by keyword). mode='session' (default,
    for a single workout) judges per-session quality; mode='weekly' checks weekly MEV/MAV/MRV
    volume landmarks. Flags push/pull imbalance, eccentric overload, order, off-target reps."""
    return _safe(lambda: coach.critique_workout(exercises, goal, mode))


@mcp.tool()
def critique_my_workout(code: str, goal: str = "general") -> dict:
    """Critique one of the user's saved Speediance workouts by its template `code`
    (from list_workouts). Fetches the live workout and runs the per-session safety review
    (a single workout is judged as a session, not against weekly volume landmarks)."""
    def run():
        c = _client(); _require_auth(c)
        detail = c.get_workout_detail(code)
        exercises = _coach_workout_to_exercises(detail)
        if not exercises:
            return {"error": "empty", "message": f"Could not parse exercises from workout {code}."}
        result = coach.critique_workout(exercises, goal, "session")
        result["parsed_exercises"] = exercises
        return result
    return _safe(run)


@mcp.tool()
def autoregulate(whoop_recovery: float = None, hrv_vs_baseline: str = None,
                 rhr_delta_bpm: float = None, sleep_hours: float = None,
                 subjective: int = None, acwr: float = None) -> dict:
    """Given today's recovery signals (any subset; from Whoop/Apple Health or a 1-5 subjective
    score), return today's training adjustment: readiness band + volume%/intensity%/RIR/session
    type. Most-conservative signal wins; sleep<6h or RHR>=+5bpm caps a green day."""
    r = {k: v for k, v in {
        "whoop_recovery": whoop_recovery, "hrv_vs_baseline": hrv_vs_baseline,
        "rhr_delta_bpm": rhr_delta_bpm, "sleep_hours": sleep_hours,
        "subjective": subjective, "acwr": acwr,
    }.items() if v is not None}
    return _safe(lambda: coach.autoregulate(r))


@mcp.tool()
def log_feedback(rpe: float = None, difficulty: str = None, completion: str = None,
                 soreness: int = None, pain: bool = None, avoid: str = None,
                 exercise: str = None, note: str = None) -> dict:
    """Tell the coach how a session went — this PERSISTS and adapts future programs in real
    time (the next generate_program automatically reflects it). Provide any subset:
    rpe (1-10), difficulty (too_easy|ok|too_hard), completion (missed|as_prescribed|exceeded),
    soreness (1-5), pain (true → flags `exercise` to avoid), avoid (exercise name to drop),
    exercise (which lift this is about), note (free text). Returns the updated profile and how
    the next program will change."""
    def run():
        c = _client()
        payload = {k: v for k, v in {
            "rpe": rpe, "difficulty": difficulty, "completion": completion,
            "soreness": soreness, "pain": pain, "avoid": avoid,
            "exercise": exercise, "note": note}.items() if v is not None}
        ev = _persist_feedback(payload, c.data_dir)
        if not ev:
            return {"error": "empty", "message": "Provide at least one feedback field."}
        prof = _feedback_profile(c.data_dir)
        return {"logged": ev, "profile": prof,
                "next_program_adjustment": coach.feedback_adjustment(prof)}
    return _safe(run)


@mcp.tool()
def get_feedback() -> dict:
    """Return the current accumulated feedback profile and how it will adjust the next
    program (avg RPE, difficulty signal, exercises being avoided)."""
    def run():
        prof = _feedback_profile(_client().data_dir)
        return {"profile": prof, "next_program_adjustment": coach.feedback_adjustment(prof)}
    return _safe(run)


@mcp.tool()
def set_readiness(whoop_recovery: float = None, hrv_vs_baseline: str = None,
                  rhr_delta_bpm: float = None, sleep_hours: float = None,
                  subjective: int = None, acwr: float = None) -> dict:
    """Persist today's recovery signals (from Whoop / Apple Health / a 1-5 subjective score)
    so subsequent generate_program / recovery calls use them automatically. Returns today's
    training adjustment."""
    def run():
        c = _client()
        payload = {k: v for k, v in {
            "whoop_recovery": whoop_recovery, "hrv_vs_baseline": hrv_vs_baseline,
            "rhr_delta_bpm": rhr_delta_bpm, "sleep_hours": sleep_hours,
            "subjective": subjective, "acwr": acwr}.items() if v is not None}
        if not payload:
            return {"error": "empty", "message": "Provide at least one readiness signal."}
        keep = _persist_readiness(payload, c.data_dir)
        return {"stored": keep, "adjustment": coach.autoregulate(keep)}
    return _safe(run)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
