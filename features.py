"""
features.py — flipch fork additions (isolated blueprint).

New endpoints:
  /healthz         - health probe (used by Docker/Uptime Kuma)
  /api/insights    - training analytics (streak, volume, frequency)  [Feature 1]
  /insights        - self-contained analytics dashboard page          [Feature 1]
  /calendar.ics    - subscribe-able iCal feed of scheduled workouts   [Feature 2]
  /api/recovery    - rest-day / recovery recommendation               [Feature 3]

All endpoints degrade gracefully when not logged in (return empty/zeroed
data rather than erroring), and parse the Speediance responses defensively
since field names vary by region/firmware.
"""
from flask import Blueprint, jsonify, Response, render_template_string, request
from datetime import datetime, date, timedelta
import json
import os
import re

import coach

features_bp = Blueprint('features', __name__)
_client = None


def init_features(client):
    global _client
    _client = client
    return features_bp


def _authed():
    return bool(_client and _client.credentials.get("token"))


def _parse_date(v):
    """Best-effort parse of a value into a date (epoch ms/s or YYYY-MM-DD-ish)."""
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)) or (isinstance(v, str) and v.strip().isdigit()):
        try:
            n = int(v)
            if n > 10_000_000_000:        # epoch millis
                n //= 1000
            if 946684800 < n < 4102444800:  # ~2000..2100
                return datetime.utcfromtimestamp(n).date()
        except Exception:
            return None
        return None
    if isinstance(v, str):
        m = re.search(r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})', v)
        if m:
            try:
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except Exception:
                return None
    return None


def _extract_date(rec):
    if not isinstance(rec, dict):
        return None
    for k, v in rec.items():
        kl = str(k).lower()
        if any(t in kl for t in ('date', 'time', 'day')):
            d = _parse_date(v)
            if d:
                return d
    return None


def _as_list(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ('list', 'records', 'data', 'items', 'rows'):
            if isinstance(data.get(key), list):
                return data[key]
        return [data]
    return []


def _history(days=90):
    if not _authed():
        return []
    end = date.today()
    start = end - timedelta(days=days)
    try:
        return _as_list(_client.get_training_history(start.isoformat(), end.isoformat()))
    except Exception:
        return []


def _session_dates(recs):
    return sorted({d for d in (_extract_date(r) for r in recs) if d})


# ---------------------------------------------------------------- health
@features_bp.route('/healthz')
def healthz():
    return jsonify({"status": "ok", "app": "smart-gym-workout-manager",
                    "authenticated": _authed()}), 200


# ----------------------------------------------------- Feature 1: insights
@features_bp.route('/api/insights')
def api_insights():
    recs = _history(90)
    dates = _session_dates(recs)
    today = date.today()
    streak = 0
    if dates:
        dset = set(dates)
        cur = today if today in dset else (today - timedelta(days=1))
        while cur in dset:
            streak += 1
            cur -= timedelta(days=1)
    last7 = sum(1 for d in dates if d >= today - timedelta(days=7))
    last30 = sum(1 for d in dates if d >= today - timedelta(days=30))
    return jsonify({
        "authenticated": _authed(),
        "window_days": 90,
        "total_sessions": len(recs),
        "active_days": len(dates),
        "current_streak_days": streak,
        "sessions_last_7d": last7,
        "sessions_last_30d": last30,
        "avg_sessions_per_week": round(last30 / (30 / 7), 1) if last30 else 0,
        "last_session": dates[-1].isoformat() if dates else None,
    })


@features_bp.route('/api/recovery')
def api_recovery():
    recs = _history(21)
    dates = _session_dates(recs)
    today = date.today()
    last7 = sum(1 for d in dates if d >= today - timedelta(days=7))
    days_since = (today - dates[-1]).days if dates else None
    if not _authed():
        rec, msg = "log_in", "Log in to get personalized recovery guidance."
    elif days_since is None or days_since >= 3:
        rec, msg = "train", "Well rested — a great day to train. 💪"
    elif last7 >= 6:
        rec, msg = "rest", "6+ sessions in 7 days. Take a rest or active-recovery day. 🛌"
    elif days_since == 0 and last7 >= 2:
        rec, msg = "active_recovery", "Already trained today — keep it light if anything. 🚶"
    else:
        rec, msg = "train", "Good to train today. ✅"
    return jsonify({"authenticated": _authed(), "recommendation": rec, "message": msg,
                    "days_since_last": days_since, "sessions_last_7d": last7})


# --------------------------------------------------- Feature 2: iCal feed
def _walk_events(node, out):
    """Recursively find dicts that carry both a date and a name -> calendar events."""
    if isinstance(node, dict):
        d = _extract_date(node)
        name = None
        for k, v in node.items():
            kl = str(k).lower()
            if isinstance(v, str) and v.strip() and any(t in kl for t in ('name', 'title')):
                name = v.strip()
                break
        if d and name:
            out.append((d, name))
        for v in node.values():
            _walk_events(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk_events(v, out)


def _build_ics(events):
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0",
             "PRODID:-//flipch//SmartGymWorkoutManager//EN",
             "CALSCALE:GREGORIAN", "X-WR-CALNAME:Smart Gym Workouts"]
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    seen = set()
    for d, name in events:
        key = (d.isoformat(), name)
        if key in seen:
            continue
        seen.add(key)
        ds = d.strftime("%Y%m%d")
        de = (d + timedelta(days=1)).strftime("%Y%m%d")
        uid = f"{ds}-{abs(hash(key)) % 10**10}@smartgym.local"
        safe = name.replace("\n", " ").replace(",", "\\,").replace(";", "\\;")
        lines += ["BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{stamp}",
                  f"DTSTART;VALUE=DATE:{ds}", f"DTEND;VALUE=DATE:{de}",
                  f"SUMMARY:🏋 {safe}", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


@features_bp.route('/calendar.ics')
def calendar_ics():
    events = []
    if _authed():
        today = date.today()
        next_month = (today.replace(day=28) + timedelta(days=10)).strftime('%Y-%m')
        for m in {today.strftime('%Y-%m'), next_month}:
            try:
                _walk_events(_client.get_calendar_month(m), events)
            except Exception:
                pass
    return Response(_build_ics(events), mimetype='text/calendar',
                    headers={'Content-Disposition': 'attachment; filename="smartgym.ics"'})


# ----------------------------------------- Feature 1 (UI): insights page
_INSIGHTS_HTML = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Insights · Smart Gym</title><style>
:root{color-scheme:dark}body{font-family:system-ui,Segoe UI,Roboto,sans-serif;background:#0f1419;color:#e6edf3;margin:0;padding:32px;max-width:880px;margin:0 auto}
h1{font-weight:650;margin:0 0 4px}.sub{color:#8b98a5;margin:0 0 28px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:16px}
.card{background:#161b22;border:1px solid #21262d;border-radius:14px;padding:20px}
.val{font-size:2.1rem;font-weight:700;line-height:1}.lbl{color:#8b98a5;font-size:.8rem;margin-top:6px;text-transform:uppercase;letter-spacing:.04em}
.rec{margin-top:24px;border-radius:14px;padding:20px;border:1px solid #21262d;background:#161b22}
.rec b{font-size:1.1rem}.pill{display:inline-block;padding:3px 10px;border-radius:999px;font-size:.75rem;font-weight:700;margin-right:8px}
.train{background:#163a2b;color:#3fb950}.rest{background:#3a2a16;color:#d29922}.active_recovery{background:#16303a;color:#39a7d2}.log_in{background:#2d333b;color:#8b98a5}
a{color:#58a6ff}.foot{margin-top:28px;color:#8b98a5;font-size:.85rem}
</style></head><body>
<h1>🏋 Workout Insights</h1><p class=sub>flipch fork · last 90 days</p>
<div class=grid id=cards></div>
<div class=rec id=rec>Loading…</div>
<p class=foot>📅 Subscribe to your schedule: <a href="/calendar.ics">/calendar.ics</a> · <a href="/">← back to app</a></p>
<script>
async function j(u){try{return await (await fetch(u)).json()}catch(e){return{}}}
(async()=>{
 const i=await j('/api/insights'), r=await j('/api/recovery');
 const cards=[['current_streak_days','Day streak'],['sessions_last_7d','This week'],
  ['sessions_last_30d','Last 30 days'],['avg_sessions_per_week','Avg / week'],
  ['active_days','Active days'],['total_sessions','Total sessions']];
 document.getElementById('cards').innerHTML=cards.map(([k,l])=>
  `<div class=card><div class=val>${i[k]??'0'}</div><div class=lbl>${l}</div></div>`).join('');
 const cls=r.recommendation||'log_in';
 document.getElementById('rec').innerHTML=
  `<span class="pill ${cls}">${cls.replace('_',' ')}</span><b>${r.message||''}</b>`+
  (i.last_session?`<div class=foot>Last session: ${i.last_session}</div>`:
   (i.authenticated?'<div class=foot>No sessions found in window.</div>':'<div class=foot>Not logged in — set up in Settings to populate.</div>'));
})();
</script></body></html>"""


@features_bp.route('/insights')
def insights_page():
    return render_template_string(_INSIGHTS_HTML)


# ═══════════════════════════════════ AI Coach (Feature 4) ═══════════════════════════════════
# Evidence-based coaching engine (coach.py) surfaced over HTTP. The same logic is exposed to
# AI agents via the MCP server and the portable Agent Skill.

def _data_dir(data_dir=None):
    """Shared /data dir. HTTP routes pass nothing (use the blueprint client); the MCP —
    a separate process with its own client — passes its client's data_dir explicitly."""
    return data_dir or (getattr(_client, "data_dir", ".") if _client else ".")


def _readiness_path(data_dir=None):
    return os.path.join(_data_dir(data_dir), "readiness.json")


def _load_readiness(data_dir=None):
    try:
        with open(_readiness_path(data_dir)) as f:
            return json.load(f)
    except Exception:
        return {}


READINESS_KEYS = ("whoop_recovery", "hrv_vs_baseline", "rhr_delta_bpm",
                  "sleep_hours", "subjective", "acwr")


def save_readiness(payload, data_dir=None):
    """Persist a readiness payload (Whoop/Apple Health/subjective) for later use."""
    keep = {k: payload[k] for k in READINESS_KEYS if k in payload}
    keep["updated"] = datetime.utcnow().isoformat() + "Z"
    with open(_readiness_path(data_dir), "w") as f:
        json.dump(keep, f)
    return keep


def _coach_workout_to_exercises(detail):
    """Best-effort map a Speediance workout detail into coach critique input."""
    out = []
    if not isinstance(detail, dict):
        return out
    actions = detail.get("actionLibraryList") or detail.get("actions") or detail.get("list") or []
    for a in actions if isinstance(actions, list) else []:
        if not isinstance(a, dict):
            continue
        name = a.get("name") or a.get("actionName") or a.get("groupName") or ""
        reps_csv = str(a.get("setsAndReps") or a.get("reps") or "")
        reps_list = [int(x) for x in re.findall(r"\d+", reps_csv)] or None
        out.append({
            "name": name,
            "sets": len(reps_list) if reps_list else int(a.get("sets") or 0),
            "reps": reps_list[0] if reps_list else a.get("rep"),
        })
    return out


@features_bp.route('/api/coach/principles')
def coach_principles():
    return jsonify(coach.principles(request.args.get("topic")))


@features_bp.route('/api/coach/readiness', methods=['GET', 'POST'])
def coach_readiness():
    """GET returns the stored readiness + today's adjustment. POST stores a readiness payload
    (the integration point for Whoop / Apple Health auto-export shortcuts)."""
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        try:
            keep = save_readiness(data)
        except Exception as e:
            return jsonify({"error": "could_not_store", "message": str(e)}), 500
        return jsonify({"stored": keep, "adjustment": coach.autoregulate(keep)})
    stored = _load_readiness()
    return jsonify({"stored": stored, "adjustment": coach.autoregulate(stored)})


@features_bp.route('/api/coach/autoregulate', methods=['POST'])
def coach_autoregulate():
    return jsonify(coach.autoregulate(request.get_json(silent=True) or {}))


@features_bp.route('/api/coach/program', methods=['POST'])
def coach_program():
    body = request.get_json(silent=True) or {}
    readiness = body.get("readiness")
    if readiness is None and body.get("use_stored_readiness", True):
        stored = _load_readiness()
        if any(k in stored for k in ("whoop_recovery", "hrv_vs_baseline",
                                     "rhr_delta_bpm", "sleep_hours", "subjective")):
            readiness = stored
    feedback = body.get("feedback")
    if feedback is None and body.get("use_stored_feedback", True):
        prof = _feedback_profile()
        feedback = prof if prof.get("events") else None
    prog = coach.generate_program(
        goal=body.get("goal", "general"),
        days_per_week=body.get("days_per_week", 4),
        experience=body.get("experience", "intermediate"),
        one_rm=body.get("one_rm"),
        readiness=readiness,
        available_accessories=body.get("available_accessories"),
        feedback=feedback,
    )
    return jsonify(prog)


# ── stateful feedback loop: agents log how a session went; the next program adapts ──
FEEDBACK_KEYS = ("exercise", "rpe", "difficulty", "completion", "soreness",
                 "pain", "avoid", "prefer", "note", "workout_code")


def _feedback_path(data_dir=None):
    return os.path.join(_data_dir(data_dir), "coach_feedback.json")


def _load_feedback_events(data_dir=None):
    try:
        with open(_feedback_path(data_dir)) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def log_feedback(payload, data_dir=None):
    """Append one feedback event (any subset of FEEDBACK_KEYS) and return the event."""
    event = {k: payload[k] for k in FEEDBACK_KEYS if k in payload}
    if not event:
        return None
    event["ts"] = datetime.utcnow().isoformat() + "Z"
    events = _load_feedback_events(data_dir)
    events.append(event)
    try:
        with open(_feedback_path(data_dir), "w") as f:
            json.dump(events[-100:], f)
    except Exception:
        pass
    return event


def _feedback_profile(data_dir=None, days=14):
    """Derive a coaching profile from recent feedback events."""
    events = _load_feedback_events(data_dir)
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat() + "Z"
    recent = [e for e in events if e.get("ts", "") >= cutoff] or events[-10:]
    rpes = [e["rpe"] for e in recent if isinstance(e.get("rpe"), (int, float))]
    avg_rpe = round(sum(rpes) / len(rpes), 1) if rpes else None
    votes = [e.get("difficulty") for e in recent if e.get("difficulty") in ("too_easy", "ok", "too_hard")]
    sig = None
    if votes:
        hard, easy = votes.count("too_hard"), votes.count("too_easy")
        sig = "too_hard" if hard > easy else "too_easy" if easy > hard else "balanced"
    sore = [e["soreness"] for e in recent if isinstance(e.get("soreness"), (int, float))]
    if sig is None and sore and sum(sore) / len(sore) >= 4:
        sig = "too_hard"
    avoid = sorted({e["avoid"] for e in events if e.get("avoid")} |
                   {e["exercise"] for e in events if e.get("pain") and e.get("exercise")})
    return {"events": len(events), "recent_events": len(recent), "avg_rpe": avg_rpe,
            "difficulty_signal": sig, "avoid": avoid,
            "recent_notes": [e["note"] for e in recent if e.get("note")][-5:]}


@features_bp.route('/api/coach/feedback', methods=['GET', 'POST'])
def coach_feedback():
    """POST a feedback event from a session (any subset of: rpe 1-10, difficulty
    too_easy|ok|too_hard, completion, soreness 1-5, pain, avoid <exercise>, note).
    GET returns the derived profile + how it will adjust the next program."""
    if request.method == 'POST':
        event = log_feedback(request.get_json(silent=True) or {})
        if not event:
            return jsonify({"error": "empty",
                            "message": "Provide at least one of: rpe, difficulty, completion, "
                                       "soreness, pain, avoid, note."}), 400
        prof = _feedback_profile()
        return jsonify({"logged": event, "profile": prof,
                        "next_program_adjustment": coach.feedback_adjustment(prof)})
    prof = _feedback_profile()
    return jsonify({"profile": prof, "next_program_adjustment": coach.feedback_adjustment(prof)})


@features_bp.route('/api/coach/critique', methods=['POST'])
def coach_critique():
    """Critique an explicit list of exercises, or — if {code} is given and authenticated —
    critique one of the user's saved Speediance workouts."""
    body = request.get_json(silent=True) or {}
    goal = body.get("goal", "general")
    exercises = body.get("exercises")
    if not exercises and body.get("code") and _authed():
        try:
            detail = _client.get_workout_detail(body["code"])
            exercises = _coach_workout_to_exercises(detail)
        except Exception as e:
            return jsonify({"error": "fetch_failed", "message": str(e)}), 502
    if not exercises:
        return jsonify({"error": "no_exercises",
                        "message": "Provide 'exercises' (list of {name,sets,reps}) or an authenticated 'code'."}), 400
    return jsonify(coach.critique_workout(exercises, goal))


_COACH_HTML = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>AI Coach · Smart Gym</title><style>
:root{color-scheme:dark}body{font-family:system-ui,Segoe UI,Roboto,sans-serif;background:#0f1419;color:#e6edf3;margin:0;padding:28px;max-width:980px;margin:0 auto}
h1{font-weight:650;margin:0 0 2px}.sub{color:#8b98a5;margin:0 0 22px}
.bar{display:flex;flex-wrap:wrap;gap:12px;align-items:end;background:#161b22;border:1px solid #21262d;border-radius:14px;padding:16px;margin-bottom:18px}
label{display:block;font-size:.72rem;text-transform:uppercase;letter-spacing:.04em;color:#8b98a5;margin-bottom:4px}
select,input{background:#0d1117;color:#e6edf3;border:1px solid #30363d;border-radius:8px;padding:8px 10px;font-size:.9rem}
button{background:#238636;color:#fff;border:0;border-radius:8px;padding:9px 16px;font-weight:600;cursor:pointer}
button.alt{background:#1f6feb}
.day{background:#161b22;border:1px solid #21262d;border-radius:14px;padding:16px 18px;margin-bottom:14px}
.day h3{margin:0 0 4px}.muted{color:#8b98a5;font-size:.82rem}
table{width:100%;border-collapse:collapse;margin-top:10px;font-size:.88rem}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #21262d}th{color:#8b98a5;font-weight:600;font-size:.72rem;text-transform:uppercase}
.pill{display:inline-block;padding:2px 8px;border-radius:999px;font-size:.72rem;font-weight:700}
.standard{background:#21303f;color:#79c0ff}.eccentric{background:#3a2a16;color:#d29922}.chain{background:#2d233f;color:#bc8cff}.constant{background:#16303a;color:#39a7d2}.spotter{background:#3d2230;color:#f778ba}
.review{border-radius:14px;padding:16px 18px;margin-bottom:14px;border:1px solid #21262d}
.warn{color:#f0883e}.ok{color:#3fb950}.score{font-size:2rem;font-weight:700}
a{color:#58a6ff}
</style></head><body>
<h1>🧠 AI Coach</h1><p class=sub>flipch fork · evidence-based program generator (see <a href="/api/coach/principles">principles</a> · citations in repo <code>knowledge/</code>)</p>
<div class=bar>
  <div><label>Goal</label><select id=goal>
    <option value=muscle>Build muscle</option><option value=strength>Build strength</option>
    <option value=fatloss>Lose fat</option><option value=general selected>General / recomp</option></select></div>
  <div><label>Days/week</label><select id=days><option>3</option><option selected>4</option><option>5</option><option>2</option></select></div>
  <div><label>Experience</label><select id=exp><option>beginner</option><option selected>intermediate</option><option>advanced</option></select></div>
  <div><label>Readiness (Whoop %)</label><input id=whoop type=number min=0 max=100 placeholder="optional" style=width:120px></div>
  <div><label>Sleep (h)</label><input id=sleep type=number step=0.5 placeholder="opt" style=width:80px></div>
  <button onclick=gen()>Generate program</button>
</div>
<div id=out class=muted>Pick options and generate a program. Runs fully offline (PT mode); add readiness to autoregulate.</div>
<script>
const $=id=>document.getElementById(id);
async function gen(){
  $('out').innerHTML='<p class=muted>Generating…</p>';
  const body={goal:$('goal').value,days_per_week:+$('days').value,experience:$('exp').value};
  const r={}; if($('whoop').value)r.whoop_recovery=+$('whoop').value; if($('sleep').value)r.sleep_hours=+$('sleep').value;
  if(Object.keys(r).length)body.readiness=r;
  const p=await (await fetch('/api/coach/program',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
  render(p);
}
function render(p){
  let h=`<div class=review style="background:#161b22"><span class=score>${p.review.score}</span> <span class=muted>/100 program quality · ${p.goal_label} · ${p.days_per_week}×/wk · ${p.experience}</span>`;
  if(p.autoregulation){h+=`<div style="margin-top:8px">Readiness: <b>${p.autoregulation.band.toUpperCase()}</b> — ${p.autoregulation.message}</div>`;}
  if(p.review.warnings.length){h+='<ul>'+p.review.warnings.map(w=>`<li class=warn>⚠ ${w}</li>`).join('')+'</ul>';}
  else{h+='<div class=ok style="margin-top:8px">✓ Volume, balance and order all within evidence-based ranges.</div>';}
  h+=`<div class=muted style="margin-top:6px">Weekly sets/muscle: ${Object.entries(p.weekly_sets_per_muscle).map(([k,v])=>k+' '+v).join(' · ')}</div></div>`;
  for(const d of p.days){
    h+=`<div class=day><h3>${d.name}</h3><div class=muted>${d.warmup} · ~${d.setup_swaps} setup changes</div>`;
    h+='<table><tr><th>Exercise</th><th>Sets×Reps</th><th>%1RM</th><th>RIR</th><th>Rest</th><th>Mode</th><th>Setup</th></tr>';
    for(const e of d.exercises){h+=`<tr><td>${e.name}${e.unilateral?' <span class=muted>(L/R)</span>':''}</td><td>${e.sets}×${e.reps}${e.load?(' @'+e.load):''}</td><td>${e.pct1rm}</td><td>${e.rir}</td><td>${e.rest_sec}s</td><td><span class="pill ${e.mode}" title="${e.mode_note}">${e.mode}</span></td><td class=muted>${e.accessory}/${e.belt}</td></tr>`;}
    h+='</table>';
    if(d.conditioning)h+=`<div class=muted style="margin-top:8px">🏃 ${d.conditioning}</div>`;
    h+='</div>';
  }
  h+=`<p class=muted>${p.disclaimer}</p>`;
  $('out').innerHTML=h;
}
</script></body></html>"""


@features_bp.route('/coach')
def coach_page():
    return render_template_string(_COACH_HTML)


# ═══════════════════════ Completed-session detail (latest workout) ═══════════════════════
# Mirrors the web History detail modal: get_training_records (list) -> get_training_detail
# (per-set actuals). Helpers are client-parameterised so the MCP (fresh client per call) and
# the HTTP route (shared blueprint client) can both use them.

TRAINING_TYPE_MAP = {1: ("Free Lift", None), 2: ("Program", "course"),
                     5: ("Custom", "custom"), 7: ("Quick", None)}


def _fmt_secs(s):
    s = int(s or 0)
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    return f"{h}h{m:02d}m" if h else f"{m}m{sec:02d}s"


def _unit_label(client):
    return "lbs" if (client and client.credentials.get("unit")) else "kg"


def _summarize_set(s, unilateral):
    d = s.get("trainingInfoDetail") or {}
    side = {1: "L", 2: "R"}.get(s.get("leftRight")) if unilateral else None
    warr = None
    if unilateral and s.get("leftRight") == 1 and d.get("leftWeights"):
        warr = d["leftWeights"]
    elif unilateral and s.get("leftRight") == 2 and d.get("rightWeights"):
        warr = d["rightWeights"]
    elif d.get("weights"):
        warr = d["weights"]
    reps = s.get("finishedCount") or 0
    tgt = s.get("targetCount") or 0
    cap = s.get("capacity") or 0
    out = {"reps": reps, "target_reps": tgt, "completed": bool(tgt and reps >= tgt),
           "volume": round(cap, 1)}
    if side:
        out["side"] = side
    if warr:
        if len(set(warr)) == 1:
            out["weight"] = warr[0]
        else:  # weight changed mid-set — summarise as "8x43, 4x30" + first→last
            segs, cw, c = [], warr[0], 1
            for w in warr[1:]:
                if w == cw:
                    c += 1
                else:
                    segs.append((cw, c)); cw, c = w, 1
            segs.append((cw, c))
            out["weight"] = [segs[0][0], segs[-1][0]]
            out["weight_note"] = ", ".join(f"{n}x{w}" for w, n in segs)
    elif reps and cap:
        out["weight"] = round(cap / reps, 1)
    if s.get("time"):
        out["duration_sec"] = s["time"]
    return out


def _summarize_session(record, detail, unit):
    exercises = []
    for ex in (detail if isinstance(detail, list) else []):
        if not isinstance(ex, dict):
            continue
        uni = ex.get("isLeftRight") == 1
        sets = [_summarize_set(s, uni) for s in (ex.get("finishedReps") or []) if isinstance(s, dict)]
        exercises.append({
            "name": ex.get("actionLibraryName") or "Exercise",
            "group_id": ex.get("actionLibraryGroupId"),
            "rating": ex.get("actionRating"),
            "unilateral": uni, "timer": ex.get("completionMethod") == 0,
            "total_volume": round(ex.get("totalCapacity") or 0, 1),
            "max_weight": ex.get("maxWeight"),
            "sets": sets,
        })
    ts = record.get("startTimestamp")
    label = TRAINING_TYPE_MAP.get(record.get("type"), (f"Type {record.get('type')}", None))[0]
    return {
        "training_id": record.get("trainingId"), "name": record.get("title") or "Workout",
        "type": label,
        "date": datetime.utcfromtimestamp(ts).isoformat() + "Z" if ts else None,
        "duration": _fmt_secs(record.get("trainingTime")), "duration_sec": record.get("trainingTime"),
        "calories": round(record.get("calorie") or 0),
        "volume": round(record.get("totalCapacity") or 0), "unit": unit,
        "exercise_count": len(exercises),
        "total_sets": sum(len(e["sets"]) for e in exercises),
        "exercises": exercises,
    }


def _detailed_records(client, days=60):
    """Recent completed sessions that have a per-set breakdown, newest first."""
    end = date.today(); start = end - timedelta(days=days)
    recs = client.get_training_records(start.isoformat(), end.isoformat())
    recs = [r for r in (recs if isinstance(recs, list) else []) if isinstance(r, dict)
            and TRAINING_TYPE_MAP.get(r.get("type"), (None, None))[1]]
    recs.sort(key=lambda r: r.get("startTimestamp") or 0, reverse=True)
    return recs


def fetch_session_detail(client, record):
    api_type = TRAINING_TYPE_MAP.get(record.get("type"), ("?", "custom"))[1] or "custom"
    detail = client.get_training_detail(record["trainingId"], api_type)
    return _summarize_session(record, detail, _unit_label(client))


@features_bp.route('/api/last_workout')
def api_last_workout():
    """Full per-set detail of a recent completed workout (newest = n=1)."""
    if not _authed():
        return jsonify({"error": "not_authenticated",
                        "message": "Log in at /settings with your Speediance account."}), 401
    days = int(request.args.get("days", 60))
    n = max(1, int(request.args.get("n", 1)))
    try:
        recs = _detailed_records(_client, days)
    except Exception as e:
        return jsonify({"error": "fetch_failed", "message": str(e)}), 502
    if len(recs) < n:
        return jsonify({"error": "not_found",
                        "message": f"Fewer than {n} detailed workouts in the last {days} days."}), 404
    return jsonify(fetch_session_detail(_client, recs[n - 1]))
