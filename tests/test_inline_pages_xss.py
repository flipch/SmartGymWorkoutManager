"""The two pages that live inside features.py (/insights and /coach) build innerHTML from vendor/user strings.

Their real <script> blocks are executed in node with a stubbed document and hostile data; the captured markup is parsed
and must contain no injected tags and no event-handler attributes.
"""
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from html.parser import HTMLParser

TOKEN = "t" * 32
os.environ["GYM_API_TOKEN"] = TOKEN
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="gym-test-data-")
os.environ.pop("GYM_AUTH_DISABLED", None)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app as gym_app  # noqa: E402

BASIC = {"Authorization": "Basic " + base64.b64encode(f"gym:{TOKEN}".encode()).decode()}
NODE = shutil.which("node")
PAYLOADS = ['"><img src=x onerror=globalThis.PWNED=1>', "'><script>globalThis.PWNED=1</script>",
            '" onmouseover="globalThis.PWNED=1" x="', "</td><td><svg onload=globalThis.PWNED=1>"]


def py_esc(text):
    """What the pages' esc() must produce."""
    return "".join({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}.get(c, c) for c in text)


class Tags(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags, self.handlers = [], []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        for k, v in attrs:
            if k.startswith("on"):
                self.handlers.append((tag, k, v))


def inspect(markup):
    t = Tags()
    t.feed(markup)
    return t


def page_script(path):
    """Every inline script of the page, in order: pages that extend layout.html get esc()/jsq() from the layout's script."""
    resp = gym_app.app.test_client().get(path, headers=BASIC)
    assert resp.status_code == 200, (path, resp.status_code)
    found = re.findall(r"<script>(.*?)</script>", resp.get_data(as_text=True), re.S)
    assert found, path
    return "\n".join(found)


HARNESS = r"""
const vm = require('vm');
const els = {};
const el = id => (els[id] ??= {textContent: '', innerHTML: '', value: ''});
globalThis.document = {getElementById: el};
globalThis.window = {};
globalThis.PWNED = 0;
const DATA = %(data)s;
globalThis.fetch = async (u, o) => ({status: DATA.__status || 200, ok: (DATA.__status || 200) < 400, json: async () => DATA[u.split('?')[0]] ?? DATA.__default ?? {}});
vm.runInThisContext(%(script)s);
(async () => {
  %(driver)s
  await new Promise(r => setTimeout(r, 80));
  const out = {pwned: globalThis.PWNED};
  for (const [k, v] of Object.entries(els)) out[k] = v.innerHTML;
  console.log(JSON.stringify(out));
})();
"""


def run_page(script, data, driver=""):
    code = HARNESS % {"data": json.dumps(data), "script": json.dumps(script), "driver": driver}
    out = subprocess.run([NODE, "-e", code], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def assert_inert(case, captured, allowed_handlers=()):
    case.assertEqual(captured["pwned"], 0)
    for key, markup in captured.items():
        if key == "pwned" or not isinstance(markup, str):
            continue
        parsed = inspect(markup)
        case.assertNotIn("img", parsed.tags, key)
        case.assertNotIn("script", parsed.tags, key)
        case.assertNotIn("svg", [t for t in parsed.tags if t == "svg" and "onload" in markup and "&lt;" not in markup], key)
        for tag, name, value in parsed.handlers:
            case.assertTrue(any(re.fullmatch(a, value or "") for a in allowed_handlers), f"{key}: {tag} {name}={value!r}")


@unittest.skipUnless(NODE, "node is required")
class InsightsPage(unittest.TestCase):
    def data(self, payload):
        return {
            "/api/insights": {
                "authenticated": True, "unit": payload, "data_note": payload, "current_streak_days": payload,
                "muscle_volume": [{"muscle": payload, "mrv": 20, "mev": payload, "mav": payload, "status": "ok",
                                   "sets_per_wk": payload, "freq_per_wk": payload}],
                "untrained_muscles": [payload],
                "lifts": [{"name": payload, "e1rm_now": payload, "trend": payload, "e1rm_delta": payload,
                           "e1rm_series": [{"e1rm": 1}, {"e1rm": 2}]}],
                "balance": {"push_sets": payload, "pull_sets": 2, "upper_sets": 1, "lower_sets": 1,
                            "verdict": payload, "push_pull_ratio": payload},
                "frequency": {"muscles_below_2x": [payload]},
                "weekly": [{"week": "2026-W43", "tonnage": payload}],
                "prs": [{"name": payload, "weight": payload}],
            },
            "/api/recovery": {"recommendation": payload, "message": payload},
        }

    def test_hostile_values_render_as_text(self):
        script = page_script("/insights")
        for payload in PAYLOADS:
            captured = run_page(script, self.data(payload))
            self.assertTrue(captured.get("deep"), "the deep analytics section must have rendered")
            assert_inert(self, captured)
            self.assertIn(py_esc(payload), captured["deep"])
            self.assertIn(py_esc(payload), captured["rec"])


COACH_HANDLERS = [r"refreshWhoop\(true\)", r"connectWhoop\(\)", r"previewDay\(\d+\)", r"confirmDay\(\d+\)",
                  r"removeAvoid\(\d+\)", r"gen\(\)"]


@unittest.skipUnless(NODE, "node is required")
class CoachPage(unittest.TestCase):
    """templates/coach.html runs in node with hostile data in every field the server can send it."""

    def program(self, payload):
        ex = {"name": payload, "unilateral": True, "sets": payload, "reps": payload, "load": 50, "primary": "chest",
              "pct1rm": 0.7, "rir": payload, "rest_sec": payload, "mode": payload, "mode_note": payload,
              "accessory": payload, "belt": payload}
        return {"review": {"score": payload, "warnings": [payload]}, "goal_label": payload,
                "days_per_week": payload, "experience": payload,
                "autoregulation": {"band": payload, "message": payload},
                "readiness_applied": {"band": payload, "note": payload,
                                      "adjustments": [{"day": 0, "exercise": payload, "field": payload, "from": payload, "to": payload}]},
                "weekly_sets_per_muscle": {payload: payload}, "avoid_unmatched": [payload],
                "readiness_error": payload, "feedback_error": payload,
                "days": [{"name": payload, "warmup": payload, "setup_swaps": payload, "conditioning": payload,
                          "exercises": [ex]}],
                "disclaimer": payload}

    def cards(self, payload):
        return {
            "/api/coach/status": {"authenticated": True, "writes_enabled": True, "unit_confirmed": True},
            "/api/coach/readiness": {
                "signals": [{"key": payload, "value": payload, "source": payload, "age_hours": payload, "stale": True},
                            {"key": "hrv_vs_baseline", "value": payload, "source": "whoop", "age_hours": 2, "stale": False}],
                "adjustment": {"band": payload, "signals": [payload], "overrides": [payload], "volume_pct": payload},
                "max_age_hours": payload},
            "/api/coach/whoop": {"whoop": {"state": "refreshing_failed", "message": payload, "notes": [payload],
                                           "last_pull": payload, "next_pull": payload, "can_connect": True}},
            "/api/coach/feedback": {"profile": {"avoid": [payload], "unreadable": payload, "avg_rpe": payload},
                                    "next_program_adjustment": {"action": payload}},
            "/api/coach/preferences": {"preferences": {}, "error": payload},
        }

    def test_cards_are_inert(self):
        script = page_script("/coach")
        for payload in PAYLOADS:
            captured = run_page(script, self.cards(payload))
            assert_inert(self, captured, allowed_handlers=COACH_HANDLERS)
            for card in ("readiness-body", "whoop-body", "avoid-body"):
                self.assertIn(py_esc(payload), captured[card], card)

    def test_whoop_states_are_inert(self):
        script = page_script("/coach")
        for payload in PAYLOADS:
            for state in ("not_configured", "needs_reconsent", "connected", "bogus"):
                data = {"/api/coach/whoop": {"whoop": {"state": state, "message": payload, "notes": [payload], "can_connect": True}}}
                captured = run_page(script, data)
                assert_inert(self, captured, allowed_handlers=COACH_HANDLERS)

    def test_program_and_preview_are_inert(self):
        script = page_script("/coach")
        for payload in PAYLOADS:
            preview = {"name": payload + "PV", "exercise_count": payload, "resolved": [], "notes": [payload + "PN"],
                       "unresolved": [{"name": payload + "PU", "reason": payload + "PR"}]}
            driver = (f"S.status = {{authenticated: true, writes_enabled: true, unit_confirmed: true}}; S.loadUnit = {json.dumps(payload)};"
                      f"S.program = {json.dumps(self.program(payload))}; S.previews[0] = {json.dumps(preview)}; renderProgram();")
            captured = run_page(script, {}, driver=driver)
            assert_inert(self, captured, allowed_handlers=COACH_HANDLERS)
            self.assertIn(py_esc(payload), captured["out"])
            for tag in ("PV", "PN", "PU", "PR"):  # the preview's own fields, not only the program's
                self.assertIn(py_esc(payload + tag), captured["out"])

    def test_save_result_is_inert(self):
        script = page_script("/coach")
        for payload in PAYLOADS:
            data = {"/api/coach/apply": {"results": [{"created": True, "name": payload, "exercise_count": payload,
                                                      "unresolved": [{"name": payload, "reason": payload}]}],
                                         "feedback_error": payload}}
            driver = (f"S.status = {{authenticated: true, writes_enabled: true, unit_confirmed: true}};"
                      f"S.program = {json.dumps(self.program('x'))}; S.previews[0] = {{name: 'n', exercise_count: 1, resolved: [], unresolved: [], notes: []}};"
                      "await confirmDay(0);")
            captured = run_page(script, data, driver=driver)
            assert_inert(self, captured, allowed_handlers=COACH_HANDLERS)
            self.assertIn(py_esc(payload), captured["apply0"])

    def test_save_error_is_inert(self):
        script = page_script("/coach")
        for payload in PAYLOADS:
            data = {"/api/coach/apply": {"results": [{"error": payload, "message": payload}], "message": payload}}
            driver = (f"S.status = {{authenticated: true, writes_enabled: true, unit_confirmed: true}};"
                      f"S.program = {json.dumps(self.program('x'))}; S.previews[0] = {{name: 'n', exercise_count: 1, resolved: [], unresolved: [], notes: []}};"
                      "await confirmDay(0);")
            captured = run_page(script, data, driver=driver)
            assert_inert(self, captured, allowed_handlers=COACH_HANDLERS)
            self.assertIn(py_esc(payload), captured["saveMsg0"])

    def test_form_messages_are_inert(self):
        script = page_script("/coach")
        for payload in PAYLOADS:
            data = {"/api/coach/readiness": {"error": "invalid_readiness", "message": payload},
                    "/api/coach/feedback": {"error": "invalid_feedback", "message": payload},
                    "/api/coach/preferences": {"error": "invalid_preferences", "message": payload}}
            driver = ("document.getElementById('m-whoop').value = '50'; document.getElementById('a-name').value = 'x';"
                      "await submitManual(); await submitAvoid(); await rememberPrefs();")
            captured = run_page(script, {**data, "__status": 400}, driver=driver)
            assert_inert(self, captured, allowed_handlers=COACH_HANDLERS)
            self.assertIn(py_esc(payload), captured["m-msg"])
            self.assertIn(py_esc(payload), captured["a-msg"])


if __name__ == "__main__":
    unittest.main()
