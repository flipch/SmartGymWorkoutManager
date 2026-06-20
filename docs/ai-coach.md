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
        wearables (Whoop / Apple Health)            Speediance account (live workouts)
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

## Wearables (Whoop / Apple Health) — readiness ingestion

The engine autoregulates from any subset of: `whoop_recovery` (0–100),
`hrv_vs_baseline` (above/within/below), `rhr_delta_bpm`, `sleep_hours`, `subjective`
(1–5), `acwr`. Most-conservative signal wins; sleep < 6 h or RHR ≥ +5 bpm caps a green day.

**Feed it once a day**, then every program/decision uses it:

```bash
# Whoop / Apple Health "Auto Export" / an iOS Shortcut posts this each morning:
curl -X POST https://gym.lan.doghous.cloud/api/coach/readiness \
  -H 'Content-Type: application/json' \
  -d '{"whoop_recovery":42,"hrv_vs_baseline":"below","rhr_delta_bpm":6,"sleep_hours":5.8}'
# -> stores it (DATA_DIR/readiness.json) and returns today's adjustment
```

- **Whoop:** Whoop API (`/v1/recovery`, `/v1/cycle`) → map `recovery_score`,
  `resting_heart_rate`, `hrv_rmssd_milli`, `sleep` → POST the fields above.
- **Apple Health:** the "Health Auto Export" app or an iOS Shortcut reads HRV (SDNN),
  RHR, and sleep → POST the same shape. No wearable? Send `subjective` 1–5 and the coach
  works in PT mode all the same.

Agents can also pass readiness inline to `generate_program(..., readiness={...})` or call
`autoregulate(...)` directly without persisting.

## Real-time feedback loop (stateful)

Agents don't just read — they can **tell the coach how a session went**, and the next
program adapts automatically. Feedback persists to `DATA_DIR/coach_feedback.json` (shared
by the app + MCP), and `generate_program` folds the derived profile in on every call.

- **MCP:** `log_feedback(rpe, difficulty, completion, soreness, pain, avoid, exercise, note)`,
  `get_feedback()`, `set_readiness(...)`.
- **HTTP:** `POST/GET /api/coach/feedback`.

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
