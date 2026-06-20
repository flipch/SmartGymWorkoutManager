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

from api_client import SpeedianceClient
from features import _session_dates  # pure helper, shared so MCP == HTTP logic

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
def delete_workout(template_id: int) -> dict:
    """Permanently delete a custom workout template by its numeric id.
    Destructive — confirm the id with list_workouts first."""
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
            name = str(ex.get("name") or ex.get("actionName") or "")
            if q and q not in name.lower():
                continue
            out.append({
                "groupId": ex.get("id") or ex.get("groupId"),
                "name": name,
                "category": ex.get("category_name"),
                "device_type": ex.get("device_type"),
            })
            if len(out) >= max(1, limit):
                break
        return out
    return _safe(run)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
