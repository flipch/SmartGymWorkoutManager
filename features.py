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
from flask import Blueprint, jsonify, Response, render_template_string
from datetime import datetime, date, timedelta
import re

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
