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
from datetime import datetime, date, timedelta, timezone
import hashlib
from collections import defaultdict
import json
import os
import re

import coach
from auth import writes_enabled
import state_io
from insights_core import clamp_days, clamp_n, recovery_advice, summarize

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
                return datetime.fromtimestamp(n).date()  # container TZ (America/Los_Angeles), not UTC
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


class HistoryUnavailable(Exception):
    """The training history could not be read (token expired, network, vendor outage)."""


def _history(days=90):
    """Sessions in the last `days` days. [] only when NOT signed in; an outage raises HistoryUnavailable so an error
    can never read as "no training -> well rested"."""
    if not _authed():
        return []
    days = clamp_days(days, 90)
    end = date.today()
    start = end - timedelta(days=days)
    try:
        return _as_list(_client.get_training_history(start.isoformat(), end.isoformat()))
    except Exception as err:
        raise HistoryUnavailable(type(err).__name__) from None


def _history_unavailable_response(err):
    return jsonify({"error": "history_unavailable",
                    "message": f"Could not read your training history ({err}); no recommendation was made."}), 503


def _session_dates(recs):
    return sorted({d for d in (_extract_date(r) for r in recs) if d})


# ---------------------------------------------------------------- health
@features_bp.route('/healthz')
def healthz():
    return jsonify({"status": "ok", "app": "smart-gym-workout-manager"}), 200  # unauthenticated: reveal nothing else


# ----------------------------------------------------- Feature 1: insights (deep)
def _iso_week(d):
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def _set_weight(s):
    w = s.get("weight")
    if isinstance(w, list):
        return max(w) if w else 0
    return w or 0


def _epley(w, reps):
    return round(w * (1 + reps / 30.0), 1)


def _working_sets(ex):
    """Count sets that were actually worked (have reps); unilateral counts L+R as 0.5 each."""
    n = sum(1 for s in ex.get("sets", []) if (s.get("reps") or _set_weight(s) > 0))
    _, _, uni = coach.classify_exercise(ex["name"])
    return n / 2.0 if uni else n


def _deep_insights(days=90, max_sessions=24):
    """Compute the rich analytics: per-muscle weekly volume vs landmarks, e1RM progression,
    balance, frequency, weekly trends, PRs, distribution — from real per-set history."""
    recs = []
    detailed = []
    try:
        detailed = _detailed_records(_client, days)
    except Exception:
        detailed = []
    sessions = []
    for r in detailed[:max_sessions]:
        try:
            sessions.append(fetch_session_detail(_client, r))
        except Exception:
            pass

    weeks = set()
    wk_muscle = defaultdict(lambda: defaultdict(float))
    muscle_days = defaultdict(set)
    push = pull = upper = lower = 0.0
    lift_series = defaultdict(list)
    prs = {}

    for sess in sorted(sessions, key=lambda s: s.get("date") or ""):
        if not sess.get("date"):
            continue
        d = date.fromisoformat(sess["date"][:10])
        wk = _iso_week(d)
        weeks.add(wk)
        for ex in sess.get("exercises", []):
            muscle, pattern, uni = coach.classify_exercise(ex["name"])
            n = _working_sets(ex)
            if muscle:
                wk_muscle[wk][muscle] += n
                muscle_days[muscle].add(sess["date"][:10])
            if pattern in ("h_push", "v_push"):
                push += n
            elif pattern in ("h_pull", "v_pull"):
                pull += n
            if muscle in ("quads", "hamstrings", "glutes", "calves"):
                lower += n
            elif muscle:
                upper += n
            # e1RM top set + PR
            best, mw = None, 0
            for s in ex.get("sets", []):
                w, reps = _set_weight(s), (s.get("reps") or 0)
                mw = max(mw, w)
                if w > 0 and reps > 0:
                    e = _epley(w, reps)
                    if best is None or e > best:
                        best = e
            if best:
                lift_series[ex["name"]].append({"date": sess["date"][:10], "e1rm": best})
            if mw > prs.get(ex["name"], 0):
                prs[ex["name"]] = mw

    nweeks = max(1, len(weeks))
    muscle_volume, distribution = [], []
    for m, (mev, mav, mrv) in coach.VOLUME.items():
        total = sum(wk_muscle[w].get(m, 0) for w in weeks)
        spw = round(total / nweeks, 1)
        if spw <= 0:
            continue
        status = "under_mev" if spw < mev else "over_mrv" if spw > mrv else "in_range"
        muscle_volume.append({"muscle": m, "sets_per_wk": spw, "mev": mev, "mav": mav,
                              "mrv": mrv, "status": status,
                              "freq_per_wk": round(len(muscle_days[m]) / nweeks, 1)})
        distribution.append({"muscle": m, "sets_per_wk": spw})
    muscle_volume.sort(key=lambda x: x["sets_per_wk"], reverse=True)
    tot_sets = sum(d["sets_per_wk"] for d in distribution) or 1
    for d in distribution:
        d["pct"] = round(100 * d["sets_per_wk"] / tot_sets)
    distribution.sort(key=lambda x: x["sets_per_wk"], reverse=True)
    untrained = [m for m in coach.VOLUME if sum(wk_muscle[w].get(m, 0) for w in weeks) == 0]

    lifts = []
    for name, series in lift_series.items():
        if len(series) < 2:
            continue
        s = series[-6:]
        delta = round(s[-1]["e1rm"] - s[0]["e1rm"], 1)
        lifts.append({"name": name, "e1rm_series": s, "e1rm_now": s[-1]["e1rm"],
                      "e1rm_delta": delta,
                      "trend": "up" if delta > 0.5 else "down" if delta < -0.5 else "flat"})
    lifts.sort(key=lambda l: -abs(l["e1rm_delta"]))

    ratio = round(push / pull, 2) if pull else None
    balance = {"push_sets": round(push), "pull_sets": round(pull), "push_pull_ratio": ratio,
               "upper_sets": round(upper), "lower_sets": round(lower),
               "verdict": ("push_biased" if ratio and ratio > 1.2 else
                           "pull_biased" if ratio and ratio < 0.8 else "balanced")}
    below_2x = sorted(m for m in muscle_days if len(muscle_days[m]) / nweeks < 2)

    weekly = defaultdict(lambda: {"sessions": 0, "tonnage": 0, "calories": 0, "minutes": 0})
    for r in (_history(days) or []):
        ts = r.get("startTimestamp")
        if not ts:
            continue
        wk = _iso_week(datetime.fromtimestamp(ts).date())
        weekly[wk]["sessions"] += 1
        weekly[wk]["tonnage"] += round(r.get("totalCapacity") or 0)
        weekly[wk]["calories"] += round(r.get("calorie") or 0)
        weekly[wk]["minutes"] += round((r.get("trainingTime") or 0) / 60)
    weekly_list = [{"week": w, **v} for w, v in sorted(weekly.items())][-8:]

    pr_list = sorted(({"name": n, "weight": w} for n, w in prs.items() if w > 0),
                     key=lambda x: -x["weight"])[:10]

    return {
        "detail_sessions": len(sessions), "detail_weeks": len(weeks),
        "data_note": (f"Per-muscle & strength metrics from {len(sessions)} detailed "
                      f"sessions over {len(weeks)} week(s)." if sessions else
                      "No per-set workout detail yet — log a Speediance workout to populate."),
        "muscle_volume": muscle_volume, "untrained_muscles": untrained,
        "lifts": lifts[:10], "balance": balance,
        "frequency": {"muscles_below_2x": below_2x},
        "weekly": weekly_list, "prs": pr_list, "volume_distribution": distribution,
    }


@features_bp.route('/api/insights')
def api_insights():
    try:
        recs = _history(90)
    except HistoryUnavailable as err:
        return _history_unavailable_response(err)
    dates = _session_dates(recs)
    out = {
        "authenticated": _authed(),
        "window_days": 90, "unit": _unit_label(_client),
        "total_sessions": len(recs),
        **summarize(dates, date.today()),
    }
    if _authed():
        try:
            out.update(_deep_insights(90))
        except Exception as e:
            out["deep_error"] = str(e)
    return jsonify(out)


@features_bp.route('/api/recovery')
def api_recovery():
    if not _authed():
        return jsonify({"authenticated": False, "recommendation": "log_in",
                        "message": "Log in to get personalized recovery guidance.",
                        "days_since_last": None, "sessions_last_7d": 0})
    try:
        recs = _history(21)
    except HistoryUnavailable as err:
        return _history_unavailable_response(err)
    return jsonify({"authenticated": True, **recovery_advice(_session_dates(recs), date.today())})


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


def _ics_text(value):
    """RFC 5545 TEXT: no control characters (a CR/LF would inject new properties), backslash first, then , ;"""
    cleaned = "".join(" " if ch in "\r\n\t" else ch for ch in str(value) if ch >= " " or ch in "\r\n\t")
    return cleaned.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;")


def _ics_fold(line):
    """Fold at 75 octets (RFC 5545 3.1) without splitting a UTF-8 character."""
    raw, out, first = line.encode("utf-8"), [], True
    while raw:
        limit = 75 if first else 74
        chunk = raw[:limit]
        while True:
            try:
                piece = chunk.decode("utf-8")
                break
            except UnicodeDecodeError:
                chunk = chunk[:-1]
        out.append(piece if first else " " + piece)
        raw, first = raw[len(chunk):], False
    return "\r\n".join(out)


def _build_ics(events, now=None):
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0",
             "PRODID:-//flipch//SmartGymWorkoutManager//EN",
             "CALSCALE:GREGORIAN", "X-WR-CALNAME:Smart Gym Workouts"]
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    seen = set()
    for d, name in events:
        key = (d.isoformat(), name)
        if key in seen:
            continue
        seen.add(key)
        ds = d.strftime("%Y%m%d")
        de = (d + timedelta(days=1)).strftime("%Y%m%d")
        # stable across restarts: hash() is salted per process, so every restart made clients duplicate every event
        uid = f"{ds}-{hashlib.sha256(repr(key).encode()).hexdigest()[:20]}@smartgym.local"
        lines += ["BEGIN:VEVENT", f"UID:{uid}", f"DTSTAMP:{stamp}",
                  f"DTSTART;VALUE=DATE:{ds}", f"DTEND;VALUE=DATE:{de}",
                  _ics_fold(f"SUMMARY:🏋 {_ics_text(name)}"), "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


@features_bp.route('/calendar.ics')
def calendar_ics():
    if not _authed():
        return Response("Not signed in to Speediance.", status=503, mimetype='text/plain')
    events, months, failed = [], set(), 0
    today = date.today()
    months = {today.strftime('%Y-%m'), (today.replace(day=28) + timedelta(days=10)).strftime('%Y-%m')}
    for m in sorted(months):
        try:
            _walk_events(_client.get_calendar_month(m), events)
        except Exception:
            failed += 1
    if failed:
        # A subscribed calendar app treats an empty/partial feed as "delete everything": better a visible 503, which
        # makes clients keep their last good copy.
        return Response("Calendar temporarily unavailable.", status=503, mimetype='text/plain',
                        headers={'Retry-After': '300'})
    return Response(_build_ics(events), mimetype='text/calendar',
                    headers={'Content-Disposition': 'attachment; filename="smartgym.ics"'})


# ----------------------------------------- Feature 1 (UI): insights page
_INSIGHTS_HTML = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Insights · Smart Gym</title><style>
:root{color-scheme:dark}body{font-family:system-ui,Segoe UI,Roboto,sans-serif;background:#0f1419;color:#e6edf3;margin:0;padding:32px;max-width:980px;margin:0 auto}
h1{font-weight:650;margin:0 0 2px}h2{font-size:1.05rem;margin:30px 0 12px;font-weight:650}.sub{color:#8b98a5;margin:0 0 6px}
.badge{display:inline-block;background:#1f2630;color:#8b98a5;border-radius:999px;padding:2px 10px;font-size:.74rem;margin-bottom:18px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:14px}
.card{background:#161b22;border:1px solid #21262d;border-radius:14px;padding:18px}
.val{font-size:1.9rem;font-weight:700;line-height:1}.lbl{color:#8b98a5;font-size:.74rem;margin-top:6px;text-transform:uppercase;letter-spacing:.04em}
.rec{margin-top:18px;border-radius:14px;padding:16px 18px;border:1px solid #21262d;background:#161b22}
.pill{display:inline-block;padding:3px 10px;border-radius:999px;font-size:.74rem;font-weight:700;margin-right:8px}
.train{background:#163a2b;color:#3fb950}.rest{background:#3a2a16;color:#d29922}.active_recovery{background:#16303a;color:#39a7d2}.log_in{background:#2d333b;color:#8b98a5}
.panel{background:#161b22;border:1px solid #21262d;border-radius:14px;padding:16px 18px}
.vrow{display:flex;align-items:center;gap:10px;margin:7px 0;font-size:.86rem}
.vname{width:90px;color:#c9d1d9;text-transform:capitalize}.vtrack{position:relative;flex:1;height:14px;background:#0d1117;border-radius:7px;overflow:hidden}
.vfill{position:absolute;left:0;top:0;bottom:0;border-radius:7px}.tick{position:absolute;top:-2px;bottom:-2px;width:2px;background:#5b6673}
.vmeta{width:120px;text-align:right;color:#8b98a5;font-size:.78rem}
.liftgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px}
.lift{background:#161b22;border:1px solid #21262d;border-radius:12px;padding:12px 14px}
.up{color:#3fb950}.down{color:#f0883e}.flat{color:#8b98a5}
.gauge{height:20px;border-radius:10px;overflow:hidden;display:flex;background:#0d1117}
.gp{background:#1f6feb}.gpull{background:#3fb950}.gl{background:#bc8cff}.glo{background:#d29922}
a{color:#58a6ff}.muted{color:#8b98a5;font-size:.82rem}.foot{margin-top:30px;color:#8b98a5;font-size:.85rem}
table{width:100%;border-collapse:collapse;font-size:.85rem}td{padding:4px 6px;border-bottom:1px solid #21262d}
</style></head><body>
<h1>🏋 Workout Insights</h1><p class=sub>flipch fork · last 90 days</p>
<div class=badge id=badge></div>
<div class=grid id=cards></div>
<div class=rec id=rec></div>
<div id=deep></div>
<p class=foot>📅 Subscribe to your schedule: <a href="/calendar.ics">/calendar.ics</a> · <a href="/coach">AI Coach →</a> · <a href="/">← back to app</a></p>
<script>
const esc=v=>String(v==null?'':v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function j(u){try{return await (await fetch(u)).json()}catch(e){return{}}}
function spark(series){const v=series.map(p=>p.e1rm),w=130,h=30,n=v.length;if(n<2)return'';
 const mn=Math.min(...v),mx=Math.max(...v),r=(mx-mn)||1;
 const pts=v.map((x,i)=>`${(i/(n-1))*w},${(h-3)-((x-mn)/r)*(h-8)}`).join(' ');
 return `<svg width=${w} height=${h} style="display:block;margin-top:6px"><polyline fill=none stroke=#58a6ff stroke-width=2 points="${pts}"/></svg>`;}
(async()=>{
 const i=await j('/api/insights'), r=await j('/api/recovery'), U=i.unit||'';
 document.getElementById('badge').textContent = i.authenticated
   ? (i.data_note||'') : 'Not logged in — set up in Settings to populate.';
 const cards=[['current_streak_days','Day streak'],['sessions_last_7d','This week'],
  ['sessions_last_30d','Last 30 days'],['avg_sessions_per_week','Avg / week'],
  ['active_days','Active days'],['total_sessions','Total sessions']];
 document.getElementById('cards').innerHTML=cards.map(([k,l])=>
  `<div class=card><div class=val>${esc(i[k]??'0')}</div><div class=lbl>${esc(l)}</div></div>`).join('');
 const cls=r.recommendation||'log_in';
 document.getElementById('rec').innerHTML=`<span class="pill ${esc(cls)}">${esc(cls.replace('_',' '))}</span><b>${esc(r.message||'')}</b>`;
 if(!i.authenticated)return;
 let h='';
 // 1) volume vs landmarks
 if((i.muscle_volume||[]).length){
  h+='<h2>🎯 Weekly volume vs landmarks <span class=muted>(sets/muscle/wk)</span></h2><div class=panel>';
  for(const m of i.muscle_volume){
   const max=m.mrv*1.12, col=m.status=='under_mev'?'#f0883e':m.status=='over_mrv'?'#d29922':'#3fb950';
   const fp=Math.min(100,100*m.sets_per_wk/max),mev=100*m.mev/max,mav=100*m.mav/max;
   const fl=m.freq_per_wk<2?` <span class=down>⚠ ${esc(m.freq_per_wk)}×/wk</span>`:` ${esc(m.freq_per_wk)}×/wk`;
   h+=`<div class=vrow><div class=vname>${esc(m.muscle.replace('_',' '))}</div>
     <div class=vtrack><div class=vfill style="width:${fp}%;background:${col}"></div>
     <div class=tick style="left:${mev}%" title="MEV ${esc(m.mev)}"></div><div class=tick style="left:${mav}%" title="MAV ${esc(m.mav)}"></div></div>
     <div class=vmeta>${esc(m.sets_per_wk)}${fl}</div></div>`;
  }
  h+='<div class=muted style="margin-top:8px">Ticks = MEV / MAV. Red below MEV (too little to grow), amber over MRV (recovery risk).</div>';
  if((i.untrained_muscles||[]).length)h+=`<div class=muted style="margin-top:4px">Untrained: ${i.untrained_muscles.map(esc).join(', ')}</div>`;
  h+='</div>';
 }
 // 2) strength progression
 if((i.lifts||[]).length){
  h+='<h2>💪 Strength progression <span class=muted>(estimated 1RM, Epley)</span></h2><div class=liftgrid>';
  for(const l of i.lifts){const a=l.trend=='up'?'▲':l.trend=='down'?'▼':'■';
   h+=`<div class=lift><div style="font-size:.86rem">${esc(l.name)}</div>
     <div style="margin-top:4px"><b style="font-size:1.3rem">${esc(l.e1rm_now)}</b> ${esc(U)}
     <span class="${esc(l.trend)}">${a} ${l.e1rm_delta>0?'+':''}${esc(l.e1rm_delta)}</span></div>${spark(l.e1rm_series)}</div>`;}
  h+='</div>';
 }
 // 3) balance
 if(i.balance && (i.balance.push_sets||i.balance.pull_sets)){
  const b=i.balance,tot=(b.push_sets+b.pull_sets)||1,ut=(b.upper_sets+b.lower_sets)||1;
  h+='<h2>⚖ Balance</h2><div class=panel>';
  h+=`<div class=muted>Push : Pull — ${esc(b.push_sets)}:${esc(b.pull_sets)} (${esc(b.push_pull_ratio??'–')}) <b class=${b.verdict=='balanced'?'up':'down'}>${esc(b.verdict.replace('_',' '))}</b></div>
   <div class=gauge style="margin:6px 0 12px"><div class=gp style="width:${100*b.push_sets/tot}%"></div><div class=gpull style="width:${100*b.pull_sets/tot}%"></div></div>
   <div class=muted>Upper : Lower — ${esc(b.upper_sets)}:${esc(b.lower_sets)}</div>
   <div class=gauge style="margin-top:6px"><div class=gl style="width:${100*b.upper_sets/ut}%"></div><div class=glo style="width:${100*b.lower_sets/ut}%"></div></div>`;
  if((i.frequency||{}).muscles_below_2x||[].length){const bl=i.frequency.muscles_below_2x||[];
   if(bl.length)h+=`<div class=muted style="margin-top:10px">Trained &lt;2×/wk: ${bl.map(esc).join(', ')}</div>`;}
  h+='</div>';
 }
 // 4) weekly trend
 if((i.weekly||[]).length){
  const mx=Math.max(...i.weekly.map(w=>w.tonnage))||1;
  h+='<h2>📈 Weekly tonnage</h2><div class=panel><div style="display:flex;align-items:flex-end;gap:10px;height:120px">';
  for(const w of i.weekly)h+=`<div style="flex:1;text-align:center">
    <div style="background:#1f6feb;border-radius:4px 4px 0 0;height:${Math.max(3,100*w.tonnage/mx)}px" title="${esc(w.tonnage)} ${esc(U)}"></div>
    <div class=muted style="font-size:.68rem;margin-top:4px">${esc(w.week.split('-')[1])}</div><div class=muted style="font-size:.66rem">${Math.round(w.tonnage/1000)}k</div></div>`;
  h+='</div><div class=muted style="margin-top:6px">Per ISO week · sessions/cal/min also tracked.</div></div>';
 }
 // 5) PRs
 if((i.prs||[]).length){
  h+='<h2>🏆 Top weights</h2><div class=panel><table>';
  for(const p of i.prs)h+=`<tr><td>${esc(p.name)}</td><td style="text-align:right">${esc(p.weight)} ${esc(U)}</td></tr>`;
  h+='</table></div>';
 }
 document.getElementById('deep').innerHTML=h||'<p class=muted style="margin-top:20px">No per-set detail yet — log a Speediance workout to unlock deep analytics.</p>';
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
    """Persist a validated readiness payload (Whoop/Apple Health/subjective). Raises ValueError (field name only)
    for a bad value, so a typo can never reach the autoregulator."""
    keep = state_io.validate_readiness(payload)
    if not keep:
        raise ValueError("no readiness signal supplied")
    keep["updated"] = state_io.utc_stamp()
    state_io.atomic_write_json(_readiness_path(data_dir), keep)
    return keep


def readiness_age_hours(stored, now=None):
    """Hours since the stored reading was written; None when it carries no readable timestamp."""
    when = state_io.parse_stamp((stored or {}).get("updated"))
    if when is None:
        return None
    return ((now or datetime.now(timezone.utc)) - when).total_seconds() / 3600


def _fresh_readiness(data_dir=None):
    """The stored readiness ONLY if it is recent: yesterday's recovery score must not autoregulate today."""
    stored = _load_readiness(data_dir)
    age = readiness_age_hours(stored)
    if age is None or age > state_io.READINESS_MAX_AGE_H or age < -1:
        return {}
    return stored


def _coach_workout_to_exercises(detail):
    """Map a Speediance workout detail into coach critique input. The display name lives under
    `title` (not `name`), and `mainMuscleGroupName` is the device's own muscle tag — pass it
    through so the classifier is anchored on ground truth. setsAndReps is one CSV entry/set."""
    out = []
    if not isinstance(detail, dict):
        return out
    actions = (detail.get("actionLibraryList") or detail.get("actionInfoList")
               or detail.get("actions") or detail.get("list") or [])
    for a in actions if isinstance(actions, list) else []:
        if isinstance(a, list):          # actionInfoList is a list-of-lists
            a = a[0] if a and isinstance(a[0], dict) else {}
        if not isinstance(a, dict):
            continue
        name = a.get("title") or a.get("name") or a.get("actionName") or a.get("groupName") or ""
        parts = [p for p in str(a.get("setsAndReps") or a.get("reps") or "").split(",") if p.strip()]
        reps = None
        if parts:
            mm = re.search(r"\d+", parts[0])
            reps = int(mm.group()) if mm else None
        out.append({
            "name": name,
            "muscle": a.get("mainMuscleGroupName"),
            "sets": len(parts) if parts else int(a.get("sets") or 0),
            "reps": reps,
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
        data = request.get_json(silent=True)
        try:
            keep = save_readiness(data)
        except ValueError as e:
            return jsonify({"error": "invalid_readiness", "message": str(e)}), 400
        except Exception as e:
            return jsonify({"error": "could_not_store", "message": type(e).__name__}), 500
        return jsonify({"stored": keep, "adjustment": coach.autoregulate(keep)})
    stored = _load_readiness()
    age = readiness_age_hours(stored)
    stale = bool(stored) and (age is None or age > state_io.READINESS_MAX_AGE_H)
    usable = {} if stale else stored
    return jsonify({"stored": stored, "age_hours": None if age is None else round(age, 1), "stale": stale,
                    "adjustment": coach.autoregulate(usable)})


@features_bp.route('/api/coach/autoregulate', methods=['POST'])
def coach_autoregulate():
    try:
        readiness = state_io.validate_readiness(request.get_json(silent=True) or {})
    except ValueError as e:
        return jsonify({"error": "invalid_readiness", "message": str(e)}), 400
    return jsonify(coach.autoregulate(readiness))


@features_bp.route('/api/coach/program', methods=['POST'])
def coach_program():
    body = request.get_json(silent=True) or {}
    readiness = body.get("readiness")
    if readiness is not None:
        try:
            readiness = state_io.validate_readiness(readiness)
        except ValueError as e:
            return jsonify({"error": "invalid_readiness", "message": str(e)}), 400
    if readiness is None and body.get("use_stored_readiness", True):
        stored = _fresh_readiness()
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


def _load_feedback_events(data_dir=None, *, quarantine_corrupt=False):
    """Stored events. A missing file is []; an unreadable one is [] for readers, and for the writer it is moved aside
    first so the next append cannot silently destroy the history."""
    path = _feedback_path(data_dir)
    try:
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, list):
            return [e for e in data if isinstance(e, dict)]
        raise ValueError("not a list")
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        if quarantine_corrupt and os.path.exists(path):
            state_io.quarantine(path)
        return []


def log_feedback(payload, data_dir=None):
    """Validate and append one feedback event (any subset of FEEDBACK_KEYS); return it, or None when `payload` carries
    nothing. Raises ValueError for a bad value and OSError/TimeoutError if the write fails: a lost event must not
    look like a logged one."""
    event = state_io.validate_feedback(payload)
    if not event:
        return None
    event["ts"] = state_io.utc_stamp()
    path = _feedback_path(data_dir)
    with state_io.file_lock(path + ".lock"):  # web app and MCP server both append
        events = _load_feedback_events(data_dir, quarantine_corrupt=True)
        events.append(event)
        state_io.atomic_write_json(path, events[-100:])
    return event


def _feedback_profile(data_dir=None, days=14):
    """Derive a coaching profile from recent feedback events."""
    events = _load_feedback_events(data_dir)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    recent = [e for e in events if (state_io.parse_stamp(e.get("ts")) or cutoff - timedelta(days=1)) >= cutoff] \
        or events[-10:]
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
        try:
            event = log_feedback(request.get_json(silent=True))
        except ValueError as e:
            return jsonify({"error": "invalid_feedback", "message": str(e)}), 400
        except (OSError, TimeoutError) as e:
            return jsonify({"error": "feedback_not_saved", "message": type(e).__name__}), 503
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
            return jsonify({"error": "fetch_failed", "message": type(e).__name__}), 502
    if not exercises:
        return jsonify({"error": "no_exercises",
                        "message": "Provide 'exercises' (list of {name,sets,reps}) or an authenticated 'code'."}), 400
    mode = body.get("mode", "session")
    return jsonify(coach.critique_workout(exercises, goal, mode))


# ── import a generated program into real Speediance workout templates ──
_EQUIP_STOP = {"cable", "dual", "handle", "handles", "barbell", "rope", "tricep", "triceps",
               "ankle", "belt", "machine", "frame", "main", "floor", "standing", "seated",
               "prone", "incline", "decline", "bench", "mat", "smith", "single", "arm", "leg",
               "the", "with", "to", "of", "and", "for", "on", "at", "in", "a", "bar", "grip",
               "wide", "close", "neutral", "v", "straight"}
_MUSCLE_ANCHOR = {
    "chest": {"pecs", "chest"}, "back": {"lats", "back", "upper back", "mid back"},
    "shoulders": {"front delts", "side delts", "shoulders", "delts"}, "rear_delts": {"rear delts"},
    "quads": {"quads", "quadriceps"}, "glutes": {"glutes"}, "hamstrings": {"hamstrings", "glutes"},
    "biceps": {"biceps", "forearms"}, "triceps": {"triceps"}, "calves": {"calves"},
    "abs": {"abs", "core"}, "traps": {"traps"},
}


def _norm_tokens(s):
    toks = re.sub(r"[^a-z0-9 ]", " ", (s or "").lower()).split()
    toks = [t for t in toks if t not in _EQUIP_STOP]
    return [t[:-1] if t.endswith("s") and len(t) > 3 else t for t in toks]


def resolve_exercise(library, name, goal_muscle=None, unilateral=False):
    """Match a coach exercise name to a real library {groupId, title, mainMuscleGroupName} via
    token overlap + muscle-anchor scoring. Returns {groupId, matched_name, muscle, confidence}."""
    coach_tokens = set(_norm_tokens(name))
    if not coach_tokens:
        return None
    anchor = _MUSCLE_ANCHOR.get((goal_muscle or "").lower(), set())
    best = None  # (key, gid, title, mg, score)
    for ex in library:
        if not isinstance(ex, dict):
            continue
        title = ex.get("title") or ex.get("name") or ""
        gid = ex.get("id") or ex.get("groupId")
        if not gid or not title:
            continue
        lib_tokens = set(_norm_tokens(title))
        overlap = len(coach_tokens & lib_tokens)
        if not overlap:
            continue
        score = overlap / len(coach_tokens) + 0.25 * overlap / len(lib_tokens)
        mg = (ex.get("mainMuscleGroupName") or "").lower()
        if anchor:
            score += 0.5 if mg in anchor else -0.15
        is_uni = ex.get("isLeftRight") == 1 or "single" in title.lower()
        if unilateral and is_uni:
            score += 0.15
        elif not unilateral and is_uni:
            score -= 0.10
        key = (score, -len(lib_tokens))
        if best is None or key > best[0]:
            best = (key, gid, title, mg, score)
    if best is None:
        cands = sorted((len(_norm_tokens(ex.get("title") or "")), ex.get("id"), ex.get("title"))
                       for ex in library if isinstance(ex, dict) and ex.get("id")
                       and (ex.get("mainMuscleGroupName") or "").lower() in anchor)
        if cands:
            return {"groupId": cands[0][1], "matched_name": cands[0][2], "confidence": "low"}
        return None
    score = best[4]
    conf = "high" if score >= 1.0 else "med" if score >= 0.55 else "low"
    return {"groupId": best[1], "matched_name": best[2], "muscle": best[3], "confidence": conf}


def program_day_to_payload(library, day):
    """Build save_workout's exercises payload for one program day + a resolution report."""
    exercises, resolved, unresolved = [], [], []
    for e in day.get("exercises", []):
        r = resolve_exercise(library, e.get("name", ""), e.get("primary"), e.get("unilateral"))
        if not r:
            unresolved.append({"name": e.get("name"), "muscle": e.get("primary")})
            continue
        n_sets = int(e.get("sets") or 0)
        sets = [{"reps": int(e.get("reps") or 0), "weight": float(e.get("load") or 0),
                 "rest": int(e.get("rest_sec") or 90), "unit": "reps"} for _ in range(n_sets)]
        exercises.append({"groupId": r["groupId"], "sets": sets})
        resolved.append({"coach_name": e.get("name"), "groupId": r["groupId"],
                         "matched_name": r["matched_name"], "confidence": r["confidence"]})
    return exercises, resolved, unresolved


MAX_WORKOUT_NAME = 80


def apply_program(client, program, day_index=None, name=None, dry_run=True):
    """Resolve + (optionally) save a generated program's day(s) as Speediance templates."""
    days = program.get("days")
    if not isinstance(days, list) or not days or not all(isinstance(d, dict) for d in days):
        raise ValueError("program.days must be a non-empty list of days")
    if day_index is None:
        targets = list(enumerate(days))
    elif isinstance(day_index, int) and not isinstance(day_index, bool) and 0 <= day_index < len(days):
        targets = [(day_index, days[day_index])]
    else:  # never fall back to "all days": that would create every workout because of one typo
        raise ValueError(f"day_index must be an integer from 0 to {len(days) - 1}")
    if name is not None and (not isinstance(name, str) or not name.strip() or len(name) > MAX_WORKOUT_NAME):
        raise ValueError(f"name must be 1-{MAX_WORKOUT_NAME} characters")
    library = client.get_library() or []
    results = []
    for idx, day in targets:
        exercises, resolved, unresolved = program_day_to_payload(library, day)
        day_label = day.get('name', 'Day ' + str(idx + 1))
        nm = ((f"{name.strip()} — {day_label}" if len(targets) > 1 else name.strip()) if name
              else f"{program.get('goal_label', 'Workout')} — {day_label}")
        entry = {"day_index": idx, "name": nm, "exercise_count": len(exercises),
                 "resolved": resolved, "unresolved": unresolved, "created": False}
        if not dry_run and exercises:
            try:
                resp = client.save_workout(nm, exercises)
                entry["created"] = True
                data = resp.get("data") if isinstance(resp, dict) else None
                if isinstance(data, dict):
                    entry["template_id"] = data.get("id") or data.get("templateId")
                    entry["code"] = data.get("code")
                elif data:
                    entry["template_id"] = data
            except Exception as e:
                entry["error"] = type(e).__name__
        results.append(entry)
    created = sum(1 for r in results if r["created"])
    failed = sum(1 for r in results if r.get("error"))
    return {"dry_run": dry_run, "results": results, "created": created, "failed": failed,
            "partial": created > 0 and failed > 0}


@features_bp.route('/api/coach/apply', methods=['POST'])
def coach_apply():
    """Import a generated program into the user's Speediance workouts. Body:
    {program:<generate_program output>, day_index?:int, name?:str, dry_run?:bool (default true)}."""
    if not _authed():
        return jsonify({"error": "not_authenticated",
                        "message": "Log in at /settings to save to your Speediance account."}), 401
    body = request.get_json(silent=True) or {}
    program = body.get("program")
    if not isinstance(program, dict) or not program.get("days"):
        return jsonify({"error": "no_program",
                        "message": "POST {program:<generate_program output>, day_index?, name?, dry_run?}"}), 400
    dry_run = body.get("dry_run") is not False  # only a literal false saves; null/0/"" stay a dry run
    if not dry_run and not writes_enabled():
        return jsonify({"error": "writes_disabled", "message": "Set GYM_ALLOW_WRITES=1 to save to Speediance."}), 403
    try:
        return jsonify(apply_program(_client, program, body.get("day_index"), body.get("name"), dry_run))
    except ValueError as e:
        return jsonify({"error": "bad_request", "message": str(e)}), 400
    except Exception as e:
        return jsonify({"error": "apply_failed", "message": type(e).__name__}), 502


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
const esc=v=>String(v==null?'':v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
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
  window.__P=p;
  let h=`<div class=review style="background:#161b22"><span class=score>${esc(p.review.score)}</span> <span class=muted>/100 program quality · ${esc(p.goal_label)} · ${esc(p.days_per_week)}×/wk · ${esc(p.experience)}</span>`;
  if(p.autoregulation){h+=`<div style="margin-top:8px">Readiness: <b>${esc(p.autoregulation.band.toUpperCase())}</b> — ${esc(p.autoregulation.message)}</div>`;}
  if(p.review.warnings.length){h+='<ul>'+p.review.warnings.map(w=>`<li class=warn>⚠ ${esc(w)}</li>`).join('')+'</ul>';}
  else{h+='<div class=ok style="margin-top:8px">✓ Volume, balance and order all within evidence-based ranges.</div>';}
  h+=`<div class=muted style="margin-top:6px">Weekly sets/muscle: ${Object.entries(p.weekly_sets_per_muscle).map(([k,v])=>esc(k)+' '+esc(v)).join(' · ')}</div></div>`;
  p.days.forEach((d,i)=>{
    h+=`<div class=day><h3>${esc(d.name)}</h3><div class=muted>${esc(d.warmup)} · ~${esc(d.setup_swaps)} setup changes</div>`;
    h+='<table><tr><th>Exercise</th><th>Sets×Reps</th><th>%1RM</th><th>RIR</th><th>Rest</th><th>Mode</th><th>Setup</th></tr>';
    for(const e of d.exercises){h+=`<tr><td>${esc(e.name)}${e.unilateral?' <span class=muted>(L/R)</span>':''}</td><td>${esc(e.sets)}×${esc(e.reps)}${e.load?(' @'+esc(e.load)):''}</td><td>${esc(e.pct1rm)}</td><td>${esc(e.rir)}</td><td>${esc(e.rest_sec)}s</td><td><span class="pill ${esc(e.mode)}" title="${esc(e.mode_note)}">${esc(e.mode)}</span></td><td class=muted>${esc(e.accessory)}/${esc(e.belt)}</td></tr>`;}
    h+='</table>';
    if(d.conditioning)h+=`<div class=muted style="margin-top:8px">🏃 ${esc(d.conditioning)}</div>`;
    h+=`<div style="margin-top:10px"><button class=alt onclick="saveDay(${Number(i)})">💾 Save to my workouts</button> <span id=save${Number(i)} class=muted></span></div>`;
    h+='</div>';
  });
  h+=`<p class=muted>${esc(p.disclaimer)}</p>`;
  $('out').innerHTML=h;
}
async function saveDay(i){
  const el=$('save'+i); el.textContent='Saving…';
  let resp;
  try{ resp=await fetch('/api/coach/apply',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({program:window.__P,day_index:i})}); }
  catch(e){ el.innerHTML='<span class=warn>Network error</span>'; return; }
  if(resp.status===401){ el.innerHTML='<span class=warn>Log in at /settings to save to your Speediance account.</span>'; return; }
  const d=await resp.json(); const r=(d.results&&d.results[0])||{};
  if(r.created){
    let m=`<span class=ok>✓ Saved "${esc(r.name)}" (${esc(r.exercise_count)} exercises)</span>`;
    if(r.unresolved&&r.unresolved.length)m+=` <span class=warn>· couldn't match: ${r.unresolved.map(u=>esc(u.name)).join(', ')}</span>`;
    el.innerHTML=m;
  } else { el.innerHTML='<span class=warn>'+esc(r.error||d.message||'Could not save')+'</span>'; }
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
        "date": datetime.fromtimestamp(ts).isoformat() if ts else None,  # local wall time, no misleading "Z"
        "duration": _fmt_secs(record.get("trainingTime")), "duration_sec": record.get("trainingTime"),
        "calories": round(record.get("calorie") or 0),
        "volume": round(record.get("totalCapacity") or 0), "unit": unit,
        "exercise_count": len(exercises),
        "total_sets": sum(len(e["sets"]) for e in exercises),
        "exercises": exercises,
    }


def _detailed_records(client, days=60):
    """Recent completed sessions that have a per-set breakdown, newest first."""
    days = clamp_days(days, 60)
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
    days = clamp_days(request.args.get("days", 60), 60)
    n = clamp_n(request.args.get("n", 1), 1)
    try:
        recs = _detailed_records(_client, days)
    except Exception as e:
        return jsonify({"error": "fetch_failed", "message": type(e).__name__}), 502
    if len(recs) < n:
        return jsonify({"error": "not_found",
                        "message": f"Fewer than {n} detailed workouts in the last {days} days."}), 404
    return jsonify(fetch_session_detail(_client, recs[n - 1]))
