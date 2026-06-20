# Recovery, Readiness & Autoregulation — Evidence-Based Knowledge File

Curated guidance for adjusting daily training from wearable data (Whoop, Apple Health, HRV)
or, when no wearable is present, from subjective readiness. Designed to be consumed by the
`recovery_recommendation` MCP tool / `/api/recovery` engine to autoregulate volume, intensity,
RIR (reps in reserve) and session type.

> **Honesty note on evidence strength.** HRV-guided training has the strongest support for
> *endurance/cardio* outcomes, and even there the performance benefit over good predetermined
> programming is **small** (see Sources). For *resistance training* autoregulation specifically,
> direct RCT evidence is thinner — the mapping below is a reasonable, conservative synthesis,
> not a proven protocol. Everything is **individual-baseline-relative**: a number only means
> something against *that athlete's* rolling baseline, not population norms.

---

## 1. Key recovery metrics and what they mean

### HRV — Heart Rate Variability (RMSSD / SDNN)
- **What it is:** beat-to-beat variation in the R–R interval. Higher resting HRV generally
  reflects greater parasympathetic (vagal) tone and good recovery; an acute drop reflects
  stress, fatigue, illness, alcohol, or incomplete recovery.
- **RMSSD** (root mean square of successive differences) is the preferred short-recording,
  vagally-mediated index used by most apps (often log-transformed as **Ln rMSSD**). **SDNN**
  (standard deviation of N-N intervals) is what Apple Health reports.
- **Derivation:** wrist/chest optical (PPG) or ECG samples R–R intervals. Whoop computes HRV
  over the night's sleep, weighting slow-wave sleep and later sleep more heavily. Apple Watch
  samples opportunistically (e.g., during Breathe sessions) and stores SDNN in ms.
- **CRITICAL — use a baseline, not a single reading.** A single morning value is noisy. Best
  practice (Plews/Buchheit): a **7-day rolling average** of Ln rMSSD, compared against the
  athlete's **normal range** = `mean ± 0.5 × SD` (the "smallest worthwhile change", SWC).
  - Inside the SWC band → recovered / adapting normally.
  - Below the band → suppressed (acute fatigue or, if persistent, maladaptation).
  - **Caution:** in *very* fit / highly parasympathetic athletes, HRV can be *suppressed at
    rest* near a saturation ceiling, so a *rise* off a high plateau can also signal fatigue.
    A high day-to-day coefficient of variation (CV) of HRV is itself a fatigue flag.

### Resting Heart Rate (RHR)
- **What it is:** heart rate at complete rest (ideally sleeping / on waking).
- **Meaning:** a *lower-than-baseline* RHR generally indicates good recovery; an *elevated*
  RHR (typically +5 bpm or more above baseline) flags fatigue, under-recovery, dehydration,
  illness, or overreaching. Like HRV, judge against the individual's baseline.
- **Derivation:** Apple Watch derives a daily resting rate by correlating background PPG
  readings with accelerometer (low-motion) data. Whoop uses sleeping heart rate.

### Sleep duration & quality
- **Meaning:** the dominant lever for recovery, hormonal regulation, and CNS readiness.
  Track *total sleep time*, *sleep efficiency* (asleep ÷ in bed), *consistency* of sleep/wake
  times, and time in deep/REM stages.
- **Whoop "Sleep Performance"** blends: sleep need met (sufficiency), consistency, efficiency,
  and physiological disturbance during sleep.
- **Apple Health** reports time in bed/asleep and stages (Core/Deep/REM) from Apple Watch sleep
  tracking.

### Whoop "Recovery %" (green / yellow / red)
- **What it is:** a single 0–100% daily readiness score.
- **Inputs (each compared to ~30-day baseline):** HRV (primary), RHR, respiratory rate, sleep
  performance, plus skin temperature, SpO₂, and menstrual phase where applicable.
- **Bands:** **Green 67–100%** (well recovered, primed to push), **Yellow 34–66%**
  (maintaining, moderate strain OK), **Red 0–33%** (rest/recovery likely needed).

### Whoop "Strain" (0–21)
- **What it is:** a logarithmic measure of cardiovascular + muscular load over the day, derived
  from time spent in heart-rate zones (and accumulated exertion). 0–9 light, 10–13 moderate,
  14–17 high, 18–21 all-out.
- **Use:** Whoop suggests a target strain that matches today's recovery; *yesterday's high
  strain* is a leading input to *today's* low recovery and should bias the engine toward easier
  programming. Strain is the closest wearable proxy to "acute load" for the ACWR logic below.

### Apple Health metrics (mapping to the same concepts)
- **HRV (SDNN, ms):** the HRV input. Build a personal baseline; SDNN values are systematically
  different from RMSSD, so never compare across apps — compare within Apple Health only.
- **Resting Heart Rate (bpm):** the RHR input.
- **Sleep (Time in Bed / Asleep / stages):** the sleep input.
- **Workouts + Active Energy (kcal) / Exercise minutes:** proxy for acute training load /
  strain when no Whoop strain score exists.
- **Caveat:** Apple Watch HRV is opportunistic and noisier than a dedicated nightly measure
  (reported mean absolute error ~20 ms for SDNN; failure rate rises sharply with movement).
  Prefer morning/at-rest readings and rolling averages.

---

## 2. Evidence on HRV-guided training

**Headline:** Guiding *daily intensity* by morning HRV (train hard when HRV is at/above
baseline; go easy/rest when it drops) produces performance gains **at least as good as, and
sometimes better than, predetermined programming** — with **fewer non-responders / less
likelihood of negative response**. The average performance advantage is **small**.

- **Kiviniemi et al. (2007):** seminal RCT. 26 moderately-fit men; HF-HRV measured each morning
  decided high- vs low-intensity/rest. HRV-guided group improved maximal running velocity with
  a more *homogeneous* (consistent) response than predefined training.
- **Vesterinen et al. (2016):** 40 recreational runners, 8 weeks, HRV-guided (EXP) vs
  predetermined (TRAD). Both improved 3000 m time and Vmax; relative gains in Vmax and
  countermovement jump were **significantly greater** in the HRV-guided group.
- **Javaloyes et al. (2018/2020):** well-trained cyclists, HRV-guided vs traditional/block
  periodization. HRV-guided group improved peak power (~+5%), power at VT2 (~+14%) and 40-min
  TT (~+7%); the comparison group did not significantly improve. Suggests daily HRV prescription
  can beat fixed periodization in trained cyclists.
- **Manresa-Rocamora et al. (2021), systematic review + meta-analysis** (8 studies, 199
  participants): HRV-guided training favored for **vagal HRV** (RMSSD/SD1 SMD ≈ **0.50**) but
  effects on **VO₂max (SMD ≈ 0.13), maximal aerobic capacity (0.20), VT2 (0.26) and endurance
  performance (0.20) were small and non-significant**. Conclusion: any advantage is "only by a
  small margin," mainly via better-protected vagal modulation and fewer bad responses.

**Limitations / weak-evidence flags (must respect):**
- Evidence base is mostly endurance running/cycling, modest sample sizes, weeks-not-years.
- Performance effect sizes are small and often non-significant.
- Almost no direct RCT evidence that HRV-autoregulating **resistance training** improves
  hypertrophy/strength — extrapolation only.
- Requires a **stable individual baseline** (≥1–2 weeks of consistent morning measurement under
  the same conditions). Population norms are not actionable; only deviation from *self* is.

---

## 3. Readiness → Action mapping (the rule the engine hard-codes)

Resolve readiness from the **best available signal** in this priority order:
**1) Whoop Recovery %  →  2) HRV vs personal baseline (7-day avg vs SWC band)  →
3) RHR vs baseline + sleep  →  4) Subjective 1–5 scale.**
When signals conflict, take the **more conservative (lower readiness)** band, and weight
**sleep** and **prior-day strain/load** heavily.

- **HIGH readiness** (Whoop green / HRV ≥ baseline+SWC / good sleep / normal-low RHR):
  - Proceed as planned **or push**: add 1 set to a primary lift, increase load ~2.5–5%, or
    lower target **RIR by 1** (e.g., RIR 2 → 1). Green-light high-CNS work (heavy compounds,
    sprints, plyometrics, true near-failure sets).
- **MODERATE readiness** (Whoop yellow / HRV inside SWC band / average sleep):
  - **Train as planned but cap intensity.** Keep **RIR ≥ 2**, hold or slightly trim top-set
    load, avoid new 1RM attempts. Volume unchanged or −10% if borderline.
- **LOW readiness** (Whoop red / HRV below SWC band / poor or short sleep / elevated RHR /
  very high prior-day strain):
  - **Reduce volume 30–50%**, reduce intensity (RIR ≥ 3, drop load ~10–20%), and
    **deprioritize high-CNS lifts** (defer heavy deadlifts/squats, sprints, max plyos). If
    multiple red flags or readiness is very low, **convert to active recovery / mobility /
    Zone-2** instead of the planned session.

---

## 4. Training-load management: ACWR, monotony & strain

- **Acute:Chronic Workload Ratio (ACWR):** acute load (≈ last 7 days) ÷ chronic load
  (≈ rolling 28-day average). A ratio of **~0.8–1.3** is the commonly-cited "sweet spot" of
  lower injury risk; spikes **> 1.5** have been associated with elevated injury risk. Use
  Whoop **Strain** (or Apple **Active Energy / Exercise minutes** / session load) as the load
  unit.
- **STRONG caveat (do not over-trust ACWR):** the sweet-spot figure and its predictive claims
  are **methodologically contested** — mathematical coupling, low sensitivity, correlational-
  not-causal origins, and the original authors walking back "predicts." Treat ACWR as a
  **soft guardrail / flag for conversation**, not a hard injury predictor.
  - Engine use: if ACWR **> 1.5**, bias toward MODERATE/LOW actions even if today's readiness
    looks fine; if chronically **< 0.8** (detraining), it is generally safe to build load
    gradually (≈ +10%/week as a rough cap).
- **Monotony & strain (Foster):** monotony = mean daily load ÷ SD of daily load (over a week);
  weekly strain = total weekly load × monotony. **High monotony** (everything the same hard
  load every day, no easy days) raises strain and maladaptation risk — so **vary daily load**
  (hard/easy waves) rather than grinding flat.
- **Overreaching / overtraining signs to watch (escalate to rest):**
  - *Functional overreaching:* short-term performance dip that **supercompensates within ≤2
    weeks** of recovery — acceptable if planned.
  - *Non-functional overreaching:* stagnation / decline persisting **up to ~4 weeks**.
  - *Overtraining syndrome:* prolonged maladaptation, performance loss, persistent fatigue,
    hormonal/immune disruption.
  - Wearable/subjective flags: **persistently suppressed HRV** (or abnormally high day-to-day
    HRV CV) despite rest, **elevated resting HR**, declining performance, poor/disturbed sleep,
    low motivation, elevated soreness, frequent illness. Multiple flags persisting > a few days
    → deload or rest, not autoregulated "push."

---

## 5. Sleep, recovery & performance

- Sleep is the primary driver of muscle recovery (growth-hormone release, glycogen
  resynthesis, CNS restoration) and of next-day performance, reaction time, accuracy and mood.
- **Sleep extension improves performance:** Mah et al. (2011), Stanford basketball players who
  extended to ~10 h in bed (≈ +111 min sleep/night) ran faster sprints and shot ~9% more
  accurately, with faster reaction time and better mood.
- **Minimum guidance:** target **7–9 h/night** for adults; athletes in heavy training benefit
  from the upper end (8–10 h) plus naps. Treat **< 6 h** or markedly poor efficiency as a LOW-
  readiness flag on its own, and **chronic** short sleep as a standing reason to cap intensity.

---

## 6. Handling MISSING data (no wearable)

When there is no HRV/Whoop/Apple data, use **subjective readiness** — it is validated and often
as sensitive as HRV for detecting fatigue. Combine a quick self-report into a single **1–5
score** (average the items, round to nearest, or take the lowest if any item is alarming):

1. **Sleep** last night (quantity + quality)
2. **Soreness / muscle readiness** (inverse — less soreness = higher score)
3. **Energy / fatigue**
4. **Mood / motivation to train**
5. **Stress** (inverse)

(Session-RPE from recent sessions — RPE × duration — also feeds the ACWR load logic above.)

Map the 1–5 score with the **same actions** as the wearable bands (see table below):
**5–4 = HIGH, 3 = MODERATE, 2–1 = LOW.**

---

## Key numbers (machine-usable)

Resolve `readiness_band` from the highest-priority available signal, then take the **most
conservative** band if signals disagree. Then apply the row.

| readiness_band | Whoop Recovery % | HRV vs baseline (7-day avg vs SWC = mean ± 0.5·SD) | RHR vs baseline | Sleep (h) | Subjective 1–5 | Volume Δ | Intensity / Load | Target RIR | Session type |
|---|---|---|---|---|---|---|---|---|---|
| **HIGH** | 67–100 (green) | ≥ baseline + 0.5·SD (above SWC band) | ≤ baseline (normal/low) | ≥ 7.5 good quality | 5–4 | +0 to **+1 set** | hold or **+2.5–5%** | **−1** (e.g., 2→1) | proceed / **push**; high-CNS OK |
| **MODERATE** | 34–66 (yellow) | within SWC band (± 0.5·SD) | ≤ +4 bpm | 6–7.5 | 3 | 0 (−10% if borderline) | hold; no PRs | **≥ 2** | train as planned, cap intensity |
| **LOW** | 0–33 (red) | < baseline − 0.5·SD (below SWC band) | **≥ +5 bpm** | < 6 or poor quality | 2–1 | **−30% to −50%** | **−10–20%** | **≥ 3** | reduce; **defer high-CNS**; or active recovery / mobility / Zone-2 |

**Override rules the function should also apply:**
- If **ACWR > 1.5** OR weekly **monotony** is high → drop one band toward LOW (guardrail, not
  hard rule — ACWR evidence is contested).
- If **sleep < 6 h** OR RHR **≥ +5 bpm** → treat as **at least MODERATE→LOW** regardless of a
  green/high HRV reading.
- If HRV/RHR/sleep flags **persist > ~3–4 days despite easy days** → suspect non-functional
  overreaching → schedule a **deload/rest**, do not autoregulate "push."
- Build chronic load gradually when detrained (**≈ +10%/week** cap); never spike acute load to
  chase a single green day.
- All thresholds are **individual-baseline-relative**; require ≥ 1–2 weeks of consistent
  morning data before trusting HRV-band logic.

---

## Sources

Real references (author, year, link). Evidence-strength flags noted.

1. Kiviniemi AM, Hautala AJ, Kinnunen H, Tulppo MP. **Endurance training guided individually by
   daily heart rate variability measurements.** *Eur J Appl Physiol.* 2007.
   https://link.springer.com/article/10.1007/s00421-007-0552-2
   — Seminal HRV-guided RCT; small sample.
2. Vesterinen V, et al. **Individual Endurance Training Prescription with Heart Rate
   Variability.** *Med Sci Sports Exerc.* 2016;48(7):1347–1354.
   https://pubmed.ncbi.nlm.nih.gov/26909534/
3. Javaloyes A, Sarabia JM, Lamberts RP, Moya-Ramon M. **Training Prescription Guided by
   Heart-Rate Variability in Cycling.** *Int J Sports Physiol Perform.* 2018 / and JSCR 2020
   (vs block periodization). https://pubmed.ncbi.nlm.nih.gov/29809080/ and
   https://journals.lww.com/nsca-jscr/fulltext/2020/06000/training_prescription_guided_by_heart_rate.3.aspx
4. Manresa-Rocamora A, et al. **HRV-Guided Training for Enhancing Cardiac-Vagal Modulation,
   Aerobic Fitness, and Endurance Performance: A Methodological Systematic Review with
   Meta-Analysis.** *Int J Environ Res Public Health.* 2021.
   https://pmc.ncbi.nlm.nih.gov/articles/PMC8507742/
   — **WEAK/SMALL effect flag:** performance SMDs ~0.13–0.26, non-significant; benefit mainly
   in vagal HRV and fewer non-responders.
5. Plews DJ, Laursen PB, Stanley J, Kilding AE, Buchheit M. **Training adaptation and heart rate
   variability in elite endurance athletes: opening the door to effective monitoring.**
   *Sports Med.* 2013 — 7-day rolling Ln rMSSD + SWC (mean ± 0.5·SD) methodology.
   https://pubmed.ncbi.nlm.nih.gov/23852425/
6. Gabbett TJ. **The training-injury prevention paradox: should athletes be training smarter and
   harder?** *Br J Sports Med.* 2016 — origin of ACWR "sweet spot."
   https://bjsm.bmj.com/content/50/5/273
7. Impellizzeri FM, et al. / Lolli L, et al. — **Critiques of the ACWR "sweet spot"**
   (mathematical coupling, low sensitivity, correlational origins).
   https://www.ncbi.nlm.nih.gov/pmc/articles/PMC7485291/
   — **CONTESTED-EVIDENCE flag:** treat ACWR as a soft guardrail only.
8. Foster C. **Monitoring training in athletes with reference to overtraining syndrome.**
   *Med Sci Sports Exerc.* 1998 — monotony & strain.
   https://pubmed.ncbi.nlm.nih.gov/9662690/
9. Meeusen R, et al. **Prevention, diagnosis and treatment of the overtraining syndrome (ECSS/
   ACSM joint consensus).** *Med Sci Sports Exerc.* 2013 — functional vs non-functional
   overreaching vs OTS definitions. https://pubmed.ncbi.nlm.nih.gov/23247672/
10. Mah CD, Mah KE, Kezirian EJ, Dement WC. **The effects of sleep extension on the athletic
    performance of collegiate basketball players.** *Sleep.* 2011;34(7):943–950.
    https://pubmed.ncbi.nlm.nih.gov/21731144/
11. WHOOP — **Recovery & Strain methodology** (HRV/RHR/respiratory/sleep inputs; green/yellow/
    red bands; 0–21 strain). https://www.whoop.com/us/en/thelocker/how-does-whoop-recovery-work-101/
    and https://support.whoop.com/s/article/WHOOP-Recovery
12. Apple — **Using Apple Watch to measure heart rate, calorimetry and activity** (HRV = SDNN
    via PPG; daily RHR via accelerometer-gated readings).
    https://www.apple.com/health/pdf/Heart_Rate_Calorimetry_Activity_on_Apple_Watch_November_2024.pdf
13. Validation of Apple Watch HRV/RHR (Series 9/Ultra 2): serial-measurement validity, error
    bounds, movement sensitivity. https://pmc.ncbi.nlm.nih.gov/articles/PMC11478500/
    — **MEASUREMENT-NOISE flag:** opportunistic wrist HRV is noisier than nightly chest/ECG.

> **Individual-variation flag (applies throughout):** all thresholds are relative to the
> athlete's own rolling baseline; very fit athletes can show parasympathetic saturation
> (suppressed resting HRV), so interpret direction-of-change in context, not absolute numbers.
