# Speediance Gym Monster — Modes & Programming Knowledge

Curated reference for a coaching engine. Goal: pick training modes intelligently and
order exercises to minimize setup time on the Speediance Gym Monster / Gym Monster 2 /
2S digital cable-resistance machine.

**Legend:** [VERIFIED] = stated by Speediance official material or corroborated by
multiple reputable sources. [UNVERIFIED] = single weak source or inferred; treat with
caution. [PRINCIPLE] = general digital-resistance / strength-training knowledge, NOT a
product-specific claim.

---

## Machine fundamentals [VERIFIED]

- **Resistance source:** motor-driven digital weight via high-strength polyethylene
  cable (not a physical stack). Resistance is applied in both the lifting (concentric)
  and lowering (eccentric) directions.
- **Motors / cables:** dual direct-drive motors (GM2/2S upgraded: 2× 800W PMSM
  permanent-magnet synchronous motors). Two independent cables → genuine **unilateral
  left/right** control; each arm can carry a different load, useful for correcting
  muscular imbalances.
- **Total resistance:** up to **220 lb / 100 kg** total; **per arm/side up to 110 lb
  (GM2)** or **130 lb (GM2S)** (~50 kg/arm). [VERIFIED]
- **Minimum / increments:** starts at **8 lb**; the *concentric-phase* weight cannot be
  set below 8 lb. Increment commonly cited as **1 lb / 0.5 kg**; one official spec sheet
  also lists an "8 LB" precision figure for a model variant — treat the fine increment as
  **~0.5–1 lb [VERIFIED range], exact step may vary by model/firmware [UNVERIFIED]**.
- **Belt / cable height positions:** the arm/locking mechanism has **10 height
  positions (GM2 upgraded: 11)**, spanning roughly **306 mm to 1656 mm**. This is the
  "belt position" notion: where the pulley arms lock determines pull angle (low / mid /
  high) and enables overhead press, front/back squat, rows, pulldowns, etc. [VERIFIED]
- **Safety sensors:** "Smart Safety System" detects unsafe motion and underpins Spotter
  mode (below). [VERIFIED]

---

## Training / resistance modes

Speediance officially exposes **5 resistance modes** (FAQ/help center): **Standard,
Chain(s), Eccentric, Constant (Isokinetic / "Fixed Speed"), and Spotter**. There is **no
verified separate "Elastic" or "Vita" mode** — see Unverified section. [VERIFIED: 5 modes]

### 1. Standard (constant resistance) [VERIFIED]
- **Mechanically:** uniform load throughout the full range of motion; feels closest to a
  barbell/dumbbell or a traditional cable stack. Digital weight is constant up and down.
- **Use when:** main/compound lifts, progressive overload, beginners learning movements,
  and **strength testing (1RM/3RM benchmarks)** because the load is predictable.
  Speediance guidance: beginners should spend the large majority (~90%) of time here
  before using advanced modes.

### 2. Chain mode [VERIFIED]
- **Mechanically:** simulates lifting with chains. **Resistance increases as you move up
  through the concentric range** (heaviest near lockout / top, lighter at the bottom),
  matching the body's ascending strength curve. (Note: one Speediance blog described it
  as heaviest at the bottom — the dominant, consistent description across official spec
  pages and reviews is *resistance increases toward the top/lockout*; encode it as
  **load rises through the concentric ROM** and flag the bottom-heavy wording as an
  outlier.)
- **Use when:** building **lockout strength** and explosive/power output (squat, bench,
  press, deadlift, row), breaking through sticking points/plateaus, sport-specific
  power (jumping/sprinting).

### 3. Eccentric (overload) mode [VERIFIED]
- **Mechanically:** **adds extra resistance on the lowering (eccentric) phase** vs. the
  lifting phase. Reported magnitudes: roughly **120–150% of the concentric load**
  (e.g., 50 lb up / ~75 lb down), or a fixed adder of "up to 14+ lb extra" on some
  movements depending on source/model. Exploits the fact that muscles are
  ~30–50% stronger eccentrically. [VERIFIED mechanism; exact % varies by source]
- **Use when:** **hypertrophy** (maximizes mechanical tension / time-under-tension),
  strength + CNS overload for advanced lifters, and **tendon strengthening/resilience**.
  Often applied as final sets or on accessory/isolation moves (biceps, triceps, rows).
- **Caution:** not for beginners; causes heavy DOMS; reduce working weight when first
  enabling. Speediance guidance: keep eccentric work to ~20–30% of weekly volume and
  support with recovery/protein. Reduce concentric load since the down-phase is heavier.

### 4. Constant / Isokinetic ("Fixed Speed") mode [VERIFIED]
- **Mechanically:** **caps/locks movement velocity** — the machine varies resistance to
  hold a steady speed regardless of how hard you push. Eliminates momentum/acceleration
  cheating; load adapts to effort at every point of the ROM. (Speediance phrases it as
  "maintain a balance of tension and contraction forces.")
- **Use when:** **rehabilitation / post-surgery / arthritis** (gentle, predictable,
  joint-safe loading), joint-stability work (rotator cuff, hips), learning new movement
  patterns, controlled hypertrophy "finishers," and core/rotational stability.
- **Caution:** speed-capped means it is **not** for max-power/explosive training; not a
  true 1RM testing tool.

### 5. Spotter (failure-safety / Assist) mode [VERIFIED]
- **Mechanically:** **patented auto-spot** — when the system detects you slowing/stalling
  or overreaching on a rep, it **automatically reduces the resistance** so you can
  complete the rep safely and re-rack without a human spotter. Must be **enabled before
  the set** (sometimes surfaced in-app as "Assist Mode").
- **Use when:** **training to/near failure safely** when lifting alone, AMRAP/last-set
  grinders, drop-set style finishers, testing close to a true max.
- **Caution:** it lowers load on demand, so it is unsuitable when you need a fixed,
  comparable load (e.g., clean 1RM testing or strict % work) — the assist confounds the
  measured load.

---

## Mode selection cheat-sheet (machine-usable)

| Mode | Mechanical effect | When to use (goal / exercise type) | Caution |
|------|-------------------|------------------------------------|---------|
| **Standard** | Constant load through full ROM; barbell-like | Main/compound lifts; progressive overload; beginners; **1RM/strength testing** (predictable load) | Default; no overload bias |
| **Chain** | Load **increases through concentric ROM** (heaviest near lockout) | **Lockout strength**, power/explosive (squat/bench/press/deadlift/row), break plateaus | Bottom is light — not for bottom-range weakness; one source claims bottom-heavy (outlier) |
| **Eccentric** | **Extra load on lowering phase** (~120–150% of concentric, or fixed adder) | **Hypertrophy**, advanced strength/CNS overload, **tendon resilience**; accessory/isolation finishers | Not for beginners; heavy DOMS; cut concentric weight; ≤~20–30% weekly volume |
| **Constant / Isokinetic** | **Speed-capped**; resistance varies to hold steady velocity; no momentum | **Rehab/post-op/arthritis**, joint stability (rotator cuff/hips), motor learning, controlled hypertrophy, core/rotational | Not for max power/explosive; not for clean 1RM testing |
| **Spotter / Assist** | **Auto-reduces load** when you stall/overreach | **Train to failure solo**, AMRAP/last sets, drop-set finishers, near-max attempts | Confounds measured load → don't use for strict %/1RM testing; enable before set |

---

## RM (rep-max), goals & counterweight

- **RM / 1RM tracking [VERIFIED]:** Speediance logs every rep, set, and load to maintain
  a **running estimate of your current RM** rather than requiring frequent max tests. A
  "Recommended Weight" / velocity-based feature auto-updates the 1RM estimate during
  training. Re-testing a true max is suggested only every ~8–12 weeks.
- **% → reps mapping [VERIFIED for the cited preset; otherwise PRINCIPLE]:** the app sets
  working weight from a target RM. Example presets reported:
  - **Gain Muscle:** ~12 reps @ 13RM (≈ **87% of 1RM**, ~1 rep shy of failure).
  - **Stamina / endurance:** ~20 reps @ 20RM (lighter, higher volume).
  - **Strength:** heavier load, lower reps (≈ 3–8 reps) for 1RM development.
  General mapping [PRINCIPLE]: strength ≈ 3–8 reps / high %1RM; hypertrophy ≈ 8–15 reps;
  endurance > 15 reps.
- **Goal modes in app [VERIFIED]:** users pick a goal — commonly surfaced as **build
  muscle / gain muscle, lose fat / lose weight (get fit), and build strength** — plus
  guided 28/90-day cycles for muscle growth, fat loss, or endurance. The app exposes
  **per-exercise weight, sets, reps, rest intervals, and presets**, all editable; free-
  lift sessions are also supported. [VERIFIED]
- **Counterweight / assist [VERIFIED concept; exact label varies]:** the digital system
  can apply an assisting force ("Assist"/Spotter auto-reduction). A persistent
  **counterweight/assist offset** to make a movement easier (e.g., assisted pull-ups) is
  consistent with the platform's digital-weight design; the precise in-app control name
  is **[UNVERIFIED]** — confirm in firmware/app before relying on an exact field.
- **Caution [VERIFIED]:** the in-app *Customize* path has been reported to incorrectly
  reset stored 1RM values — be careful editing RM directly.

---

## Setup-ordering rules

Switching **belt/arm height** and swapping **accessories** (handles ↔ barbell ↔ rope ↔
ankle straps ↔ bench) costs real setup time between exercises. A smart program should
sequence to minimize these swaps. [PRINCIPLE, grounded in the machine's 10/11-position
belt + clip-on accessory design — VERIFIED hardware basis]

1. **Group by accessory.** Keep all exercises that use the same attachment adjacent
   (e.g., do all rope work together, all barbell work together, all ankle-strap work
   together). Each attachment change = re-clip + often a re-calibration.
2. **Group by belt/cable height.** Within an accessory block, order by pulley height so
   you move monotonically (e.g., all **high-pulley** pulldowns/triceps → **mid** rows/
   presses → **low** curls/deadlifts), avoiding repeated up/down arm repositioning.
3. **Group by bench/standing state.** Cluster bench-based moves (bench in place) vs.
   standing/floor moves to avoid moving the bench in and out repeatedly.
4. **Order modes to avoid load whiplash.** Within a superset/circuit, keep the same mode
   where possible; if mixing, do heavy Standard/Chain work before Eccentric finishers,
   and isolate Spotter/Constant (rehab/failure) blocks so safety settings don't have to
   be toggled mid-superset.
5. **Place setup-heavy compounds (barbell squat/press) early** when fresh, since they
   need the most rigging (barbell + hooks + pad + specific height); strip down to
   simpler handle/rope accessory work later.
6. **Unilateral pairing.** Keep left/right single-arm variants of the same movement back-
   to-back (same handle, same height) to exploit the dual-cable independence without
   re-rigging.

**Heuristic cost order (highest→lowest swap cost):** bench in/out > barbell rig
(bar + hooks + pad) > belt-height change > handle/rope/strap swap > mode toggle (software,
near-zero). Optimize the program to change the expensive things least often.

---

## Accessories (official, package-dependent) [VERIFIED]

Common attachments across packages: **2× handles (smart handles), tricep rope, 2× ankle
straps, adjustable barbell + barbell pad + 2× barbell hooks, extension/extender straps,
Bluetooth ring (rep counter, with clip), yoga mat.** Higher tiers (Works Plus / Family)
add a **flat/adjustable bench** and a **rowing bench**; some bundles list **ski handles**.
Exact contents vary by package (Works / Works Plus / Family / Family Plus).

---

## Unverified / not found — do not assert as product facts

- **"Elastic" mode:** no verified standalone elastic mode. Chain mode delivers the
  variable/ascending-resistance ("band-like") feel; an explicit "Elastic" setting is
  **[UNVERIFIED]**.
- **"Vita" mode:** no evidence found in official or reputable sources. **[UNVERIFIED] —
  do not encode.**
- **Exact eccentric overload %:** sources disagree (120–150% vs. fixed "+14 lb");
  treat the magnitude as approximate / movement-dependent.
- **Chain bottom-vs-top loading:** dominant description is load rises toward the top; one
  official blog says heaviest at the bottom. Encode "rises through concentric ROM" but
  verify on-device for a given exercise.
- **Exact fine weight increment** (0.5 lb vs 1 lb vs an "8 LB" precision figure on one
  spec sheet) varies by model/firmware — confirm per device.
- **Named "counterweight" control:** the digital assist exists; the exact UI label is
  unconfirmed.

---

## Sources

**Official Speediance (VERIFIED product facts):**
- Variable Resistance Training (Standard/Chain/Eccentric/Constant): https://www.speediance.com/blogs/fitness/variable-resistance-training
- FAQ / Help Center (5 modes incl. Spotter; belt heights 306–1656 mm, 10 positions; 8 lb min): https://www.speediance.com/pages/faq
- Gym Monster 2 product/spec page (motors, per-arm load, increments, accessories): https://www.speediance.com/products/speediance-gym-monster-2
- Gym Monster 2 spec/feature page: https://www.speediance.com/pages/gym-monster-2
- Equipment overview (accessories, 10/11-level height, modes): https://www.speediance.com/pages/equipment
- Repetition Maximum (RM) — when to increase weight: https://www.speediance.com/blogs/fitness/repetition-maximum-rm-when-to-increase-weight
- Velocity-based "Recommended Weight" feature (v1.36.1): https://www.speediance.com/blogs/news/al-coach-recommended-weights-based-on-velocity-based-training
- Weight loss vs muscle building FAQ (goal guidance): https://www.speediance.com/blogs/fitness/weight-loss-and-muscle-building
- Speediance app (App Store): https://apps.apple.com/us/app/speediance-home-workout/id1612755038

**Reputable reviews / third-party (corroboration + some [UNVERIFIED] specifics):**
- TobyOnFitnessTech — modes explained (eccentric "+14 lb", goal presets, Customize 1RM-reset warning): https://tobyonfitnesstech.com/blog/speediance-2s-modes-explained/
- The Body Blueprint — training modes & workout guide (Spotter mode, programming framework, sample sessions): https://thebodyblueprint.com/speediance-training-modes-workout-guide/
- Garage Gym Reviews: https://www.garagegymreviews.com/speediance-review
- GearJunkie review: https://gearjunkie.com/health-fitness/speediance-gym-monster-2-review
- Yahoo Health review: https://health.yahoo.com/article/speediance-gym-monster-2-review-220957917.html
- Connect the Watts review: https://connectthewatts.com/2024/11/01/review-speediance-gym-monster-2/
- Speediance user manual (manuals.plus): https://manuals.plus/asin/B0CXJL8G7W

**General principles (NOT product-specific):** the rep-range→goal mapping (strength
3–8 / hypertrophy 8–15 / endurance 15+), eccentric-for-hypertrophy, isokinetic-for-rehab,
chain-for-lockout, and the setup-ordering heuristics are standard strength-training
knowledge applied to this machine's verified hardware (dual cable, 10/11 belt heights,
clip-on accessories).
