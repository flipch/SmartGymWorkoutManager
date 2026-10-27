# AI Coach — architecture, protocols & wearables

The AI Coach turns a generic agent (Claude, Codex, Cursor, an ACP/MCP host) into an
**evidence-based personal trainer** for the Speediance Gym Monster. It is deliberately
*not* an LLM baked into the container — the user's own agent is the brain; this fork
supplies the **knowledge, the deterministic engine, and the open protocols** to drive it.

```
            knowledge/*.md  (≈60 cited sources)          coach.py  (deterministic engine)
                    │                                          │
                    ▼                                          ▼
   ┌──────────────────────────────────────────────────────────────────────┐
   │  exposure layer                                                        │
   │   • HTTP   /api/coach/* + /coach page         (gym.lan.doghous.cloud)  │
   │   • MCP    generate_program / critique / autoregulate / principles     │
   │            + account tools (gym-mcp.lan.doghous.cloud/mcp)             │
   │   • Skill  skills/smart-gym-coach/SKILL.md   (portable, any agent)     │
   └──────────────────────────────────────────────────────────────────────┘
                    ▲                                          ▲
        WHOOP (pulled by the app) / manual            Speediance account (live workouts)
```

## The engine (`coach.py`)

Pure-Python, dependency-free, unit-testable. Encodes the cited corpus:
- **Volume landmarks** (MEV/MAV/MRV per muscle), rep ranges, RIR, rest, frequency.
- **Goal schemes** (muscle / strength / fatloss / general) and **3/4/5-day splits**.
- **Speediance mode selection** (standard/chain/eccentric/constant/spotter) and
  **setup-ordering** (cluster by accessory + belt height to cut machine re-rigging).
- **Autoregulation**: readiness signals → train / hold / recover.

Three jobs: `generate_program`, `critique_workout` (the "don't train the wrong things"
safety net — volume, balance, eccentric share, order, rep ranges), and `autoregulate`.

## Protocols — how an agent invokes it (the "open" part)

| Path | Transport | Who | How |
|------|-----------|-----|-----|
| **MCP** | streamable-http | Claude Code/Desktop, Agent SDK, Cursor, Zed, Codex (MCP shim) | attach `https://gym-mcp.lan.doghous.cloud/mcp` |
| **Agent Skill** | file (SKILL.md) | any skill-capable agent | drop `skills/smart-gym-coach/` into the agent's skills dir |
| **ACP** | Agent Client Protocol | ACP-speaking editors/hosts | the host attaches the MCP server above; ACP carries the session, MCP carries the tools |
| **HTTP** | REST/JSON | anything (curl, shortcuts, scripts) | `POST /api/coach/program` etc. |

MCP is the concrete, open interop layer; the Agent Skill is the portable *capability*
description; ACP hosts ride on top of the MCP. All four reach the **same** engine, so
behaviour is identical whether you ask Claude, Codex, a shell script, or the web page.

## Readiness and WHOOP

The engine autoregulates from any subset of: `whoop_recovery` (0–100),
`hrv_vs_baseline` (above/within/below), `rhr_delta_bpm`, `sleep_hours`, `subjective`
(1–5), `acwr`. The most conservative signal wins; sleep < 6 h or RHR ≥ +5 bpm caps a green day.

**WHOOP is a soft veto, nothing more.** WHOOP recovery is not validated for strength training.
A low score can cut today's volume (about 40%) and load (about 15%) and add reps in reserve; a high
score never adds volume or load. The Coach page applies readiness to a program as a reduce-only
overlay and lists every change. A signal older than 36 hours is ignored (the page shows it with a
"Stale" badge). WHOOP yields only `whoop_recovery` (scored recoveries only) and `sleep_hours`; an
unscored or pending state yields no reading, never 0.

Each stored signal records where it came from (`source`: `whoop` or `manual`) and when it was
**measured** (WHOOP readings keep their measurement time, so a repeated pull cannot make yesterday's
score look fresh). A newer manual entry is never overwritten by an older WHOOP reading. A reading
posted by hand may carry `"source": "manual"`; `"source": "whoop"` is only accepted for
`whoop_recovery` and `sleep_hours`. Files written before provenance existed keep working (the source
shows as "not recorded").

```bash
curl -X POST https://gym.lan.doghous.cloud/api/coach/readiness \
  -H 'Authorization: Bearer <GYM_API_TOKEN>' -H 'Content-Type: application/json' \
  -d '{"subjective":2,"sleep_hours":5.8,"source":"manual"}'
```

### The gym app pulls WHOOP itself

The web app and the MCP server (separate processes on the same data volume) pull WHOOP through
`whoop_sync.py`, which reuses `harness/whoop.py` and `harness/whoop_client.py`. Nothing else calls WHOOP.

- A pull happens when the Coach page opens, when an MCP readiness tool is used, and on the
  **Refresh from WHOOP** button; at most every 15 minutes (`WHOOP_PULL_MIN_INTERVAL_S`), with a
  5 s timeout (`WHOOP_HTTP_TIMEOUT_S`). A failure never blocks a page: the card says
  "WHOOP unavailable: ...", the last readings stay in use, and retries slow down (2, 4, 8 ... up to 60 minutes).
- WHOOP rotates **both** tokens on every refresh. Refreshes run behind the token store's OS file
  lock, so the web and MCP processes can never refresh at once; the loser reuses the winner's tokens.
- A dead refresh token puts the app in `needs_reconsent`: it stops retrying and the Coach page and
  the MCP `get_readiness` tool say so. Reconnect with the **Reconnect WHOOP** button, or ask an
  agent to call the MCP tool `whoop_connect`, which returns a one-time link.
- Status words (card and MCP): `not_configured`, `connected`, `refreshing_failed`, `needs_reconsent`,
  plus the last pull time. `not_configured` is a normal state: the coach works without WHOOP.

Configuration (all on **both** compose services; empty means WHOOP is off):

| Variable | Meaning |
|----------|---------|
| `WHOOP_CLIENT_ID`, `WHOOP_CLIENT_SECRET` | the WHOOP developer app's credentials (or `WHOOP_CREDS_FILE`, a mode-0600 file) |
| `WHOOP_REDIRECT_URI` | `https://gym.lan.doghous.cloud/whoop/callback`, registered in the WHOOP developer dashboard |
| `WHOOP_TOKEN_FILE` | optional; default `/data/whoop.token.json` on the shared volume (mode 0600) |
| `WHOOP_PULL_MIN_INTERVAL_S` | optional; default 900 |
| `WHOOP_HTTP_TIMEOUT_S` | optional; default 5 |

Put the first three in forge's `.env` (never in the repo). Register the redirect URI exactly as
above in the WHOOP developer dashboard (app settings, Redirect URLs). That name resolves to a private
address, so approve the consent from a browser on the home network or Tailscale. The public `gymmcp`
hostname does **not** serve the callback.

### Connecting WHOOP from the app

`Connect WHOOP` on the Coach page, or the MCP tool `whoop_connect`, creates a one-time link
(unguessable state, valid 10 minutes, single use, at most 5 pending). Open it, approve, and WHOOP
sends the browser to `GET /whoop/callback`. That one route is the only path served without the
Basic-auth password (WHOOP's redirect cannot send it); it does nothing unless the `state` is one
the app created a moment ago, it exchanges the code on the server, writes the tokens 0600 through the
token store, and shows a page with no code or token on it. `whoop_connect` creates no link while WHOOP
is healthy unless called with `force=true`.

### Moving the tokens to forge safely

The owner's tokens may still be on the Mac (`~/.config/speediance-spike/whoop.token.json`). WHOOP
tokens rotate, so exactly **one** place may ever refresh them: move them, never copy-and-run-both.

1. Make an offline backup of the Mac's token file (a USB stick or an encrypted note), before anything else.
2. On the Mac, stop everything that uses WHOOP (any `python -m harness.whoop_auth --check` loop, cron, launchd).
3. Copy `whoop.token.json` to forge into the data volume: `/data/whoop.token.json` inside the containers,
   owned by the app's user, `chmod 600`.
4. Set `WHOOP_CLIENT_ID`, `WHOOP_CLIENT_SECRET`, `WHOOP_REDIRECT_URI` in forge's `.env` and restart both services.
5. Open the Coach page: the WHOOP card should say Connected after the first pull.
6. Delete the token file on the Mac (keep the offline backup until the server has refreshed once and works).
   From then on the Mac must never refresh again. If the Mac and forge ever both refreshed, one pair is dead:
   use **Reconnect WHOOP** to re-consent.

If you would rather not move anything, skip steps 1–3 and 6 and use **Connect WHOOP** on forge for a fresh consent;
the old Mac tokens then stop working, so stop using them.

Apple Health is not supported yet.

## Real-time feedback loop (stateful)

Agents don't just read — they can **tell the coach how a session went**, and the next
program adapts automatically. Feedback persists to `DATA_DIR/coach_feedback.json` (shared
by the app + MCP), and `generate_program` folds the derived profile in on every call.

- **MCP:** `log_feedback(rpe, difficulty, completion, soreness, pain, avoid, exercise, note)`,
  `get_feedback()`, `set_readiness(...)`, `get_readiness()` (signals, source, age, WHOOP status), `whoop_connect()`.
- **HTTP:** `POST/GET /api/coach/feedback`; `DELETE /api/coach/feedback?avoid=<exercise>` takes an exercise off the avoid list
  (the Coach page has an Avoid list card for this).

How the profile steers the next program:

| Signal | Effect on next `generate_program` |
|--------|-----------------------------------|
| `difficulty=too_hard` or avg RPE ≥ 9 | −1 set/muscle, +1 RIR (back off) |
| `difficulty=too_easy` or avg RPE ≤ 6 | +1 set/muscle, −1 RIR (progress) |
| `pain` on an exercise, or `avoid=<name>` | that exercise is dropped from selection |
| `soreness` ≥ 4 (avg) | treated as too-hard |

So a session like *"log_feedback(difficulty='too_hard', avoid='Lat pulldown')"* immediately
yields a lighter next week with that lift removed — a closed real-time loop, not a one-shot.

## Applying a generated plan to the machine

The coach proposes; the user approves; then the agent writes it:
1. `search_exercises(name)` → resolve each exercise's real library `groupId`.
2. build `[{groupId, sets:[{reps, weight, rest, unit}]}]`.
3. `save_workout(name, exercises)` → a real Gym Monster template; optionally
   `schedule_workout(day, code)`.

## Safety / honesty

Educational, evidence-based programming — **not medical advice**. Citations live in
`knowledge/`. Debated/individual points (HRV-guided lifting, ACWR sweet-spot, higher-rep
%1RM conversions, lean-mass-in-deficit volume) are flagged in the corpus and the engine
treats them as soft guardrails.

## The Coach page (`/coach`)

`templates/coach.html` (extends `layout.html`): a Readiness card (each signal with value, source, age and a stale badge;
today's band with the reasons; a manual override), a WHOOP card (status, last and next pull, Refresh, Connect), a program
builder with remembered preferences (`/api/coach/preferences`, with a Reset), per-day program cards showing what readiness
changed, the unresolved exercises with the reason, and **Apply to machine** as a preview (dry run) first and then an explicit
confirm. Saving respects `GYM_ALLOW_WRITES` and the unit confirmation on Settings.
