# flipch fork — additions

This fork of [ANPC86/SmartGymWorkoutManager](https://github.com/ANPC86/SmartGymWorkoutManager)
adds a containerised homelab deployment, an MCP server, and three new features,
while keeping the upstream app fully intact.

## Deployment (Docker)

One image, two services (`docker compose up -d --build`):

| Service          | Port | What                                            |
|------------------|------|-------------------------------------------------|
| `smart-gym-app`  | 5001 | Flask web UI (gunicorn, single worker)          |
| `smart-gym-mcp`  | 5002 | MCP server (streamable-http at `/mcp`)          |

Both mount a shared `gym-data` volume at `/data` (`DATA_DIR`). The web UI
writes the logged-in user's Speediance token to `/data/config.json`; the MCP
reads the same file, so **logging in through the web UI authenticates the MCP
too** — no token plumbing required.

- `GET /healthz` → liveness probe used by the Docker healthcheck.

## MCP server (`mcp_server.py`)

Lets AI agents read and manage the logged-in user's workouts, schedule and
training history. The client is re-read from `config.json` on every call, so
login/logout in the UI is reflected immediately.

Tools: `whoami`, `list_workouts`, `get_workout`, `save_workout`,
`delete_workout`, `get_calendar`, `schedule_workout`, `training_history`,
`training_stats`, `workout_insights`, `recovery_recommendation`,
`search_exercises`.

Point an MCP client at `http://<host>:5002/mcp` (streamable-http).

## New features

1. **Insights dashboard** — `/insights` (+ JSON `/api/insights`). Training
   streak, weekly/monthly frequency, active days, last session, over 90 days.
2. **iCal feed** — `/calendar.ics`. Subscribe to your scheduled workouts from
   Apple/Google Calendar.
3. **Recovery recommendation** — `/api/recovery` (surfaced on the Insights
   page). Train / rest / active-recovery suggestion from recent frequency.
4. **AI Coach** — `/coach` (+ `/api/coach/program|critique|autoregulate|readiness|principles`).
   An evidence-based program generator + safety critic + readiness autoregulator built on a
   cited science corpus (`knowledge/*.md`, ~60 sources) and a deterministic engine
   (`coach.py`). Maps goals → Speediance modes (chain/eccentric/constant/spotter), belt/
   accessory ordering, 3/4/5-day splits, RM-based loads, and wearable (Whoop/Apple Health)
   autoregulation. Exposed identically over **HTTP, the MCP, and a portable open Agent
   Skill** (`skills/smart-gym-coach/SKILL.md`) so Claude/Codex/any agent can drive it.
   See `docs/ai-coach.md`.

The MCP now exposes 17 tools (workouts/schedule/history + the 5 coach tools).
All new endpoints degrade gracefully when logged out (zeroed data, valid empty
calendar) and parse Speediance responses defensively.
