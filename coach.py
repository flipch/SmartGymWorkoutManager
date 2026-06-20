"""
coach.py — evidence-based coaching engine for SmartGymWorkoutManager (flipch fork).

Pure-Python, dependency-free so it can be imported by both the Flask app and the
MCP server (and unit-tested in isolation). It turns goals + constraints into
concrete Speediance Gym Monster programs, critiques existing workouts, and
autoregulates a session from wearable/subjective readiness.

Every constant traces to the cited corpus in knowledge/*.md:
  - hypertrophy.md            -> volume landmarks, rep ranges, RIR, rest, balance
  - programming.md            -> goal schemes, splits, rep<->%1RM, exercise order
  - recovery-autoregulation.md -> readiness -> action mapping
  - speediance-modes.md       -> mode cheat-sheet, setup-ordering, belt positions

This is the "PT brain" an agent (via MCP or the open Agent Skill) reasons with;
the numbers are the engine's, the judgement/conversation is the agent's.
"""
from __future__ import annotations

from collections import Counter

# ───────────────────────────────────────────── goal prescriptions (programming.md §Key numbers)
GOALS = {
    "muscle":   {"label": "Build muscle (hypertrophy)", "reps": (6, 12), "pct1rm": (0.67, 0.80),
                 "rest": (90, 180), "rir": (1, 3), "conditioning": 0},
    "strength": {"label": "Build strength",             "reps": (3, 6),  "pct1rm": (0.80, 0.92),
                 "rest": (180, 300), "rir": (1, 3), "conditioning": 0},
    "fatloss":  {"label": "Lose fat (retain muscle)",   "reps": (8, 12), "pct1rm": (0.70, 0.85),
                 "rest": (75, 120),  "rir": (1, 3), "conditioning": 2},
    "general":  {"label": "General health / recomp",    "reps": (6, 12), "pct1rm": (0.67, 0.80),
                 "rest": (90, 180),  "rir": (1, 3), "conditioning": 1},
}

# ───────────────────────────────────────────── weekly volume landmarks, sets/muscle/wk (hypertrophy.md)
# stored as (MEV, MAV_low, MRV) — the responsive 10-20 band lives between MEV and MRV.
VOLUME = {
    "chest": (10, 16, 22), "back": (12, 18, 26), "quads": (8, 14, 22), "hamstrings": (6, 12, 18),
    "glutes": (8, 14, 18), "shoulders": (8, 16, 22), "rear_delts": (6, 12, 18),
    "biceps": (8, 14, 20), "triceps": (6, 12, 18), "calves": (8, 12, 18), "abs": (6, 12, 20),
}
# experience -> where to start in the band, and how much advanced machine-mode use is allowed
EXPERIENCE = {
    "beginner":     {"vol_anchor": "mev",  "advanced_modes": False, "ex_per_muscle": 1},
    "intermediate": {"vol_anchor": "low",  "advanced_modes": True,  "ex_per_muscle": 2},
    "advanced":     {"vol_anchor": "mav",  "advanced_modes": True,  "ex_per_muscle": 2},
}

# rep -> %1RM (programming.md / NSCA chart); interpolated between points.
REP_PCT = {1: 1.00, 2: 0.95, 3: 0.93, 4: 0.90, 5: 0.87, 6: 0.85, 8: 0.80, 10: 0.75, 12: 0.70, 15: 0.65}

# weekly splits (programming.md §Key numbers). day -> focus muscle groups.
SPLITS = {
    1: [("Full body", ["quads", "back", "chest", "hamstrings", "shoulders", "biceps", "triceps"])],
    2: [("Upper", ["chest", "back", "shoulders", "triceps", "biceps"]),
        ("Lower", ["quads", "hamstrings", "glutes", "calves", "abs"])],
    3: [("Full body A — squat focus", ["quads", "glutes", "chest", "back", "abs"]),
        ("Full body B — hinge focus", ["hamstrings", "glutes", "back", "shoulders", "biceps", "triceps"]),
        ("Full body C — push/pull", ["chest", "back", "shoulders", "quads", "calves"])],
    4: [("Upper A (strength lean)", ["chest", "back", "shoulders", "triceps", "biceps"]),
        ("Lower A (strength lean)", ["quads", "glutes", "hamstrings", "calves", "abs"]),
        ("Upper B (hypertrophy lean)", ["chest", "back", "shoulders", "triceps", "biceps", "rear_delts"]),
        ("Lower B (hypertrophy lean)", ["quads", "glutes", "hamstrings", "calves", "abs"])],
    5: [("Push", ["chest", "shoulders", "triceps"]),
        ("Pull", ["back", "rear_delts", "biceps"]),
        ("Legs", ["quads", "hamstrings", "glutes", "calves"]),
        ("Upper", ["chest", "back", "shoulders", "biceps", "triceps"]),
        ("Lower", ["quads", "hamstrings", "glutes", "calves", "abs"])],
}

# ───────────────────────────────────────────── Speediance mode cheat-sheet (speediance-modes.md)
MODES = {
    "standard":  "Constant load through full ROM (barbell-like). Default; progressive overload; 1RM testing.",
    "chain":     "Load rises through the concentric ROM (heaviest near lockout). Lockout strength, power, plateaus.",
    "eccentric": "Extra load on the lowering phase (~120-150%). Hypertrophy + tendon resilience; high fatigue, keep <=~25% of weekly volume.",
    "constant":  "Speed-capped (isokinetic); no momentum. Rehab/joint-safe, motor learning, controlled finishers.",
    "spotter":   "Auto-reduces load when you stall. Train to failure solo / AMRAP / drop-sets. Confounds measured load.",
}

# accessory swap-cost order (speediance-modes.md §Setup-ordering); lower = set up earlier / cheaper to keep
ACCESSORY_COST = {"barbell": 5, "bench": 4, "ankle": 3, "rope": 2, "handles": 1, "none": 0}
BELT_RANK = {"low": 0, "mid": 1, "high": 2, "na": 1}
ORDER_PRIORITY = {"heavy_compound": 1, "compound": 2, "large_iso": 3, "small_iso": 4}

# ───────────────────────────────────────────── exercise catalog (Speediance-appropriate; offline PT mode)
# muscles[0] is the primary (volume is credited there); accessory + belt drive setup-ordering.
def _ex(name, muscles, pattern, accessory, belt, prio, mode="standard", unilateral=False):
    return {"name": name, "muscles": muscles, "pattern": pattern, "accessory": accessory,
            "belt": belt, "priority": prio, "default_mode": mode, "unilateral": unilateral}

CATALOG = [
    # chest
    _ex("Cable chest press", ["chest", "triceps"], "h_push", "handles", "mid", "heavy_compound"),
    _ex("Incline cable press", ["chest", "shoulders"], "h_push", "handles", "low", "compound"),
    _ex("Cable fly", ["chest"], "h_push", "handles", "mid", "small_iso", mode="eccentric"),
    # back
    _ex("Lat pulldown", ["back", "biceps"], "v_pull", "barbell", "high", "heavy_compound"),
    _ex("Seated cable row", ["back", "biceps"], "h_pull", "handles", "mid", "heavy_compound"),
    _ex("Straight-arm pulldown", ["back"], "v_pull", "rope", "high", "small_iso"),
    _ex("Single-arm row", ["back"], "h_pull", "handles", "mid", "compound", unilateral=True),
    # shoulders / rear delts
    _ex("Cable overhead press", ["shoulders", "triceps"], "v_push", "barbell", "low", "heavy_compound"),
    _ex("Cable lateral raise", ["shoulders"], "v_push", "handles", "low", "small_iso", unilateral=True),
    _ex("Face pull", ["rear_delts"], "h_pull", "rope", "high", "small_iso"),
    _ex("Reverse cable fly", ["rear_delts"], "h_pull", "handles", "mid", "small_iso"),
    # quads / glutes
    _ex("Cable front squat", ["quads", "glutes"], "squat", "barbell", "low", "heavy_compound"),
    _ex("Bulgarian split squat", ["quads", "glutes"], "squat", "handles", "low", "compound", unilateral=True),
    _ex("Leg extension (ankle)", ["quads"], "knee", "ankle", "low", "large_iso", mode="eccentric"),
    _ex("Cable hip thrust", ["glutes", "hamstrings"], "hinge", "barbell", "low", "compound"),
    _ex("Cable glute kickback", ["glutes"], "hinge", "ankle", "low", "small_iso", unilateral=True),
    # hamstrings
    _ex("Romanian deadlift", ["hamstrings", "glutes"], "hinge", "barbell", "low", "heavy_compound"),
    _ex("Cable pull-through", ["hamstrings", "glutes"], "hinge", "rope", "low", "compound"),
    _ex("Leg curl (ankle)", ["hamstrings"], "knee", "ankle", "low", "large_iso", mode="eccentric"),
    # arms
    _ex("Cable curl", ["biceps"], "h_pull", "barbell", "low", "small_iso"),
    _ex("Hammer curl (rope)", ["biceps"], "h_pull", "rope", "low", "small_iso"),
    _ex("Triceps pushdown", ["triceps"], "v_push", "rope", "high", "small_iso"),
    _ex("Overhead triceps ext.", ["triceps"], "v_push", "rope", "low", "small_iso", mode="eccentric"),
    # calves / abs
    _ex("Standing calf raise", ["calves"], "knee", "handles", "low", "small_iso"),
    _ex("Cable crunch", ["abs"], "core", "rope", "high", "small_iso"),
    _ex("Pallof press", ["abs"], "core", "handles", "mid", "small_iso", unilateral=True),
]

PUSH_PATTERNS = {"h_push", "v_push"}
PULL_PATTERNS = {"h_pull", "v_pull"}


# ───────────────────────────────────────────── small helpers
def pct_for_reps(reps):
    """%1RM for a target rep count, linearly interpolated within the NSCA chart."""
    reps = max(1, min(20, int(reps)))
    if reps in REP_PCT:
        return REP_PCT[reps]
    pts = sorted(REP_PCT)
    lo = max(p for p in pts if p <= reps)
    hi = min(p for p in pts if p >= reps)
    if lo == hi:
        return REP_PCT[lo]
    f = (reps - lo) / (hi - lo)
    return round(REP_PCT[lo] + f * (REP_PCT[hi] - REP_PCT[lo]), 3)


def working_weight(one_rm, reps):
    """Suggested working load (kg/lb, same unit as one_rm) for reps near target RIR."""
    if not one_rm:
        return None
    return round(one_rm * pct_for_reps(reps), 1)


def _anchor_volume(muscle, anchor):
    mev, mav, mrv = VOLUME.get(muscle, (8, 12, 18))
    return {"mev": mev, "low": (mev + mav) // 2, "mav": mav, "mrv": mrv}[anchor]


# ───────────────────────────────────────────── autoregulation (recovery-autoregulation.md)
_BANDS = {"low": 0, "moderate": 1, "high": 2}
_BAND_NAME = {0: "low", 1: "moderate", 2: "high"}


def autoregulate(readiness):
    """Resolve a readiness band from any available signals (most-conservative wins) and
    return today's training adjustment. `readiness` keys (all optional):
      whoop_recovery (0-100), hrv_vs_baseline ('above'|'within'|'below'),
      rhr_delta_bpm (vs baseline), sleep_hours, subjective (1-5), acwr.
    """
    r = readiness or {}
    votes = []
    notes = []

    w = r.get("whoop_recovery")
    if isinstance(w, (int, float)):
        votes.append(2 if w >= 67 else 1 if w >= 34 else 0)
        notes.append(f"Whoop recovery {w}%")
    hrv = (r.get("hrv_vs_baseline") or "").lower()
    if hrv in ("above", "within", "below"):
        votes.append({"above": 2, "within": 1, "below": 0}[hrv])
        notes.append(f"HRV {hrv} baseline")
    rhr = r.get("rhr_delta_bpm")
    if isinstance(rhr, (int, float)):
        votes.append(2 if rhr <= 0 else 1 if rhr <= 4 else 0)
        notes.append(f"RHR {rhr:+g} bpm vs baseline")
    sl = r.get("sleep_hours")
    if isinstance(sl, (int, float)):
        votes.append(2 if sl >= 7.5 else 1 if sl >= 6 else 0)
        notes.append(f"sleep {sl} h")
    sub = r.get("subjective")
    if isinstance(sub, (int, float)):
        votes.append(2 if sub >= 4 else 1 if sub >= 3 else 0)
        notes.append(f"subjective readiness {sub}/5")

    if not votes:
        band = 1
        notes.append("no readiness data — defaulting to MODERATE (train as planned)")
    else:
        band = min(votes)  # most conservative

    overrides = []
    # hard guardrails (recovery-autoregulation.md override rules)
    if (isinstance(sl, (int, float)) and sl < 6) or (isinstance(rhr, (int, float)) and rhr >= 5):
        if band == 2:
            band = 1
            overrides.append("sleep<6h or RHR>=+5bpm caps the day at MODERATE despite a green signal")
    if isinstance(r.get("acwr"), (int, float)) and r["acwr"] > 1.5 and band > 0:
        band -= 1
        overrides.append(f"ACWR {r['acwr']} > 1.5 → dropped one band (soft guardrail)")

    table = {
        2: {"volume_pct": +10, "intensity_pct": +3, "rir_min": 1, "session": "push",
            "message": "Well recovered — proceed or push; high-CNS lifts OK."},
        1: {"volume_pct": 0, "intensity_pct": 0, "rir_min": 2, "session": "as_planned",
            "message": "Train as planned; hold intensity, no PRs."},
        0: {"volume_pct": -40, "intensity_pct": -15, "rir_min": 3, "session": "reduce_or_recover",
            "message": "Low readiness — cut volume ~40%, drop load ~15%, defer high-CNS work or do active recovery."},
    }
    out = dict(table[band])
    out["band"] = _BAND_NAME[band]
    out["signals"] = notes
    out["overrides"] = overrides
    return out


# ───────────────────────────────────────────── exercise selection & ordering
def _order(exercises):
    """Order a day's exercises: training priority first (compounds while fresh), then cluster
    by accessory + belt height to minimise Speediance setup swaps (speediance-modes.md)."""
    return sorted(exercises, key=lambda e: (
        ORDER_PRIORITY.get(e["priority"], 3),
        -ACCESSORY_COST.get(e["accessory"], 0),   # keep the rigged-up heavy accessory together early
        BELT_RANK.get(e["belt"], 1),
        e["name"],
    ))


def _swap_count(ordered):
    swaps = 0
    for a, b in zip(ordered, ordered[1:]):
        if a["accessory"] != b["accessory"]:
            swaps += 1
        elif a["belt"] != b["belt"]:
            swaps += 1
    return swaps


def _pick_for_day(focus, goal, experience, allow_eccentric):
    """Choose exercises covering the day's focus muscles, preferring compounds that hit
    several focus muscles, capped at a sensible session size."""
    per_muscle = EXPERIENCE[experience]["ex_per_muscle"]
    chosen, covered = [], {}
    # pass 1: one compound per focus muscle (prefer multi-focus compounds)
    for m in focus:
        if covered.get(m, 0) >= 1:
            continue
        cands = [e for e in CATALOG if e["muscles"][0] == m and e["priority"] in ("heavy_compound", "compound")]
        cands.sort(key=lambda e: (-len(set(e["muscles"]) & set(focus)), ORDER_PRIORITY[e["priority"]], e["name"]))
        if cands:
            e = cands[0]
            chosen.append(e)
            for mm in e["muscles"]:
                covered[mm] = covered.get(mm, 0) + 1
    # pass 2: add isolation to top up under-covered focus muscles
    if per_muscle >= 2:
        for m in focus:
            if covered.get(m, 0) >= 2:
                continue
            cands = [e for e in CATALOG if e["muscles"][0] == m and e not in chosen]
            if goal in ("muscle", "general") and allow_eccentric:
                # prefer an eccentric-overload isolation as the hypertrophy finisher
                cands.sort(key=lambda e: (0 if e["default_mode"] == "eccentric" else 1,
                                          ORDER_PRIORITY[e["priority"]], e["name"]))
            else:
                cands.sort(key=lambda e: (ORDER_PRIORITY[e["priority"]], e["name"]))
            if cands:
                e = cands[0]
                if not allow_eccentric and e["default_mode"] == "eccentric":
                    e = {**e, "default_mode": "standard"}
                chosen.append(e)
                covered[m] = covered.get(m, 0) + 1
    return chosen


def _sets_for(muscle, days_hitting, anchor, vol_pct=0):
    """Per-session sets for a muscle = weekly target / weekly frequency, capped 6-8/session."""
    weekly = _anchor_volume(muscle, anchor) * (1 + vol_pct / 100.0)
    per_session = weekly / max(1, days_hitting)
    return max(2, min(7, round(per_session)))


# ───────────────────────────────────────────── program generation
def generate_program(goal="general", days_per_week=4, experience="intermediate",
                     one_rm=None, readiness=None, available_accessories=None):
    """Generate a full weekly Speediance program.

    one_rm: optional {muscle_or_exercise_name: 1RM} to fill working loads.
    readiness: optional dict (see autoregulate) → scales today's volume/intensity/RIR.
    available_accessories: optional list to restrict the catalog (PT-mode constraint).
    Returns a structured dict (JSON-serialisable).
    """
    goal = goal if goal in GOALS else "general"
    days_per_week = max(1, min(5, int(days_per_week)))
    experience = experience if experience in EXPERIENCE else "intermediate"
    g = GOALS[goal]
    exp = EXPERIENCE[experience]
    one_rm = one_rm or {}

    # Autoregulation is a *daily* overlay on top of the baseline week — it is reported as
    # guidance (band + how to adjust today's session), not applied to the weekly volume,
    # so the generated program keeps proper MEV→MRV volume year-round.
    auto = autoregulate(readiness) if readiness else None

    split = SPLITS[days_per_week]
    # weekly frequency per muscle across the chosen split
    freq = {}
    for _, focus in split:
        for m in focus:
            freq[m] = freq.get(m, 0) + 1

    rep_lo, rep_hi = g["reps"]
    target_reps = rep_lo if goal == "strength" else (rep_lo + rep_hi) // 2
    days_out = []
    weekly_sets = {}

    for name, focus in split:
        if available_accessories:
            focus = focus  # focus unchanged; catalog filter happens in selection
        picks = _pick_for_day(focus, goal, experience, exp["advanced_modes"])
        if available_accessories:
            picks = [e for e in picks if e["accessory"] in set(available_accessories) | {"none"}] or picks
        ordered = _order(picks)
        # split each muscle's per-session set target across the exercises that hit it
        counts = Counter(e["muscles"][0] for e in ordered)
        targets = {m: _sets_for(m, freq.get(m, 1), exp["vol_anchor"]) for m in counts}
        seen = {m: 0 for m in counts}
        exercises = []
        for e in ordered:
            m = e["muscles"][0]
            seen[m] += 1
            n, tgt = counts[m], targets[m]
            base = tgt // n
            sets = max(2, base + (1 if seen[m] <= (tgt - base * n) else 0))
            # mode: beginners stay Standard; strength compounds may use Chain; eccentric finishers for muscle
            mode = "standard"
            if exp["advanced_modes"]:
                if goal == "strength" and e["priority"] == "heavy_compound":
                    mode = "chain"
                elif goal in ("muscle", "general") and e["default_mode"] == "eccentric":
                    mode = "eccentric"
            reps = target_reps
            load = working_weight(one_rm.get(e["name"]) or one_rm.get(m), reps)
            exercises.append({
                "name": e["name"], "primary": m, "muscles": e["muscles"],
                "sets": sets, "reps": reps, "pct1rm": round(pct_for_reps(reps), 2),
                "load": load, "rir": g["rir"][0], "rest_sec": g["rest"][0],
                "mode": mode, "mode_note": MODES[mode],
                "accessory": e["accessory"], "belt": e["belt"], "unilateral": e["unilateral"],
            })
            weekly_sets[m] = weekly_sets.get(m, 0) + sets
        day = {
            "name": name, "focus": focus,
            "warmup": "RAMP: 5-10 min raise + activate/mobilize, then 2-4 ramp-up sets on the first compound.",
            "exercises": exercises,
            "setup_swaps": _swap_count(ordered),
        }
        if g["conditioning"]:
            day["conditioning"] = f"Optional finisher: {8 if goal=='fatloss' else 6}-12 min circuit / Zone-2."
        days_out.append(day)

    review = critique_program(weekly_sets, days_out, goal)
    return {
        "goal": goal, "goal_label": g["label"], "days_per_week": days_per_week,
        "experience": experience, "split": "/".join(d["name"].split(" ")[0] for d in days_out),
        "prescription": {"reps": list(g["reps"]), "pct1rm": list(g["pct1rm"]),
                          "rest_sec": list(g["rest"]), "rir": list(g["rir"])},
        "autoregulation": auto,
        "days": days_out,
        "weekly_sets_per_muscle": weekly_sets,
        "review": review,
        "disclaimer": "Educational, evidence-based programming — not medical advice. See knowledge/ for citations.",
    }


# ───────────────────────────────────────────── critique / safety ("not training wrong things")
def critique_program(weekly_sets, days, goal="general"):
    """Check weekly volume vs landmarks, push/pull balance, eccentric share, and order."""
    findings, warnings = [], []

    for m, (mev, mav, mrv) in VOLUME.items():
        s = weekly_sets.get(m, 0)
        if s == 0:
            continue
        if s < mev:
            warnings.append(f"{m}: {s} sets/wk is below MEV ({mev}) — likely too little to grow.")
        elif s > mrv:
            warnings.append(f"{m}: {s} sets/wk exceeds MRV ({mrv}) — recovery risk, trim volume.")
    # muscles entirely missing that the goal usually wants
    for m in ("back", "hamstrings", "rear_delts"):
        if weekly_sets.get(m, 0) == 0:
            findings.append(f"No direct {m} volume — common imbalance; add a movement.")

    # push/pull balance across the week
    push = pull = ecc = total = 0
    for d in days:
        for e in d["exercises"]:
            total += 1
            pat = next((x["pattern"] for x in CATALOG if x["name"] == e["name"]), "")
            if pat in PUSH_PATTERNS:
                push += e["sets"]
            elif pat in PULL_PATTERNS:
                pull += e["sets"]
            if e.get("mode") == "eccentric":
                ecc += e["sets"]
    allsets = sum(weekly_sets.values()) or 1
    if push and pull and push > pull * 1.2:
        warnings.append(f"Push:pull = {push}:{pull} sets — bias toward MORE pulling (target ~1:1 to 1:2).")
    if ecc / allsets > 0.30:
        warnings.append(f"Eccentric-overload is {round(100*ecc/allsets)}% of volume — keep <=~25-30% (high fatigue).")

    # exercise order: isolation should not precede a compound for the same region within a day
    for d in days:
        prios = [ORDER_PRIORITY.get(next((x["priority"] for x in CATALOG if x["name"] == e["name"]), "compound"), 3)
                 for e in d["exercises"]]
        if prios != sorted(prios):
            findings.append(f"{d['name']}: exercise order isn't compound→isolation (auto-ordered, double-check).")

    score = max(0, 100 - 12 * len(warnings) - 4 * len(findings))
    return {"score": score, "warnings": warnings, "notes": findings,
            "balance": {"push_sets": push, "pull_sets": pull, "eccentric_share_pct": round(100 * ecc / allsets)}}


def critique_workout(exercises, goal="general"):
    """Critique an ad-hoc/imported workout: a list of dicts with at least
    {name|muscle, sets, reps} (mode optional). Returns findings + suggestions."""
    weekly = {}
    norm_days = [{"name": "session", "exercises": []}]
    for ex in (exercises or []):
        name = ex.get("name") or ex.get("exercise") or ""
        cat = next((x for x in CATALOG if x["name"].lower() == name.lower()), None)
        muscle = (ex.get("muscle") or (cat["muscles"][0] if cat else "") or "").lower()
        sets = int(ex.get("sets") or 0)
        if muscle:
            weekly[muscle] = weekly.get(muscle, 0) + sets
        norm_days[0]["exercises"].append({
            "name": name or muscle, "sets": sets, "reps": ex.get("reps"),
            "mode": (ex.get("mode") or (cat["default_mode"] if cat else "standard")),
        })
    g = GOALS.get(goal, GOALS["general"])
    rep_flags = []
    for ex in (exercises or []):
        reps = ex.get("reps")
        if isinstance(reps, (int, float)) and not (g["reps"][0] <= reps <= g["reps"][1] + 4):
            rep_flags.append(f"{ex.get('name','exercise')}: {reps} reps is off-target for {goal} ({g['reps'][0]}-{g['reps'][1]}).")
    rev = critique_program(weekly, norm_days, goal)
    rev["rep_range_flags"] = rep_flags
    rev["weekly_sets_per_muscle"] = weekly
    return rev


def principles(topic=None):
    """Return a compact, citable principle summary the agent can speak from."""
    base = {
        "volume": "10-20 hard sets/muscle/wk drives hypertrophy (diminishing returns past ~10); per-muscle MEV/MAV/MRV in knowledge/hypertrophy.md.",
        "intensity": "Hypertrophy works ≥~30% 1RM near failure; strength needs ≥80% 1RM, 1-6 reps.",
        "rir": "Train most sets at 0-3 RIR; strength 1-3 RIR. ~1-2 RIR captures most growth.",
        "frequency": "Hit each muscle ≥2x/week; use frequency to distribute volume.",
        "rest": "Compounds 2-3 min, isolation 1-2 min; don't short-rest heavy work.",
        "balance": "Bias pulling ≥ pushing (~1:1 to 1:2); train antagonists; don't skip hamstrings/rear delts.",
        "modes": "Standard for main lifts; Chain for lockout/power; Eccentric for hypertrophy (<=25% volume); Constant for rehab; Spotter to fail safely.",
        "ordering": "Compounds first; cluster by accessory + belt height to minimise Speediance setup swaps.",
        "recovery": "Autoregulate from Whoop/HRV/sleep/subjective: green→push, yellow→hold, red→cut volume ~40% / load ~15%.",
        "fatloss": "Diet drives the deficit; keep training load/volume HIGH to retain muscle; add conditioning, don't 'tone'.",
    }
    if topic:
        return {topic: base.get(topic, "unknown topic")}
    return base
