"""Vendor-controlled strings must not break out of HTML or inline-JS contexts (review round 12: XSS).

Server-rendered templates are rendered with hostile data and their `onclick` attributes are EXECUTED in node against
stubbed handlers: a breakout sets globalThis.PWNED. The JS-built markup (innerHTML) is covered by a lint that pins
every previously vulnerable interpolation to an escaped form, plus execution of the shared esc()/jsq() helpers.
"""
import html as htmllib
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

ROOT = os.path.join(os.path.dirname(__file__), "..")
TEMPLATES = os.path.join(ROOT, "templates")
NODE = shutil.which("node")

# Each payload closes a JS string / call and runs code, or closes an attribute / tag.
PAYLOADS = ["');globalThis.PWNED=1;//", "\");globalThis.PWNED=1;//", "'-globalThis.PWNED=1-'",
            "</script><script>globalThis.PWNED=1</script>", "\"><img src=x onerror=globalThis.PWNED=1>",
            "${globalThis.PWNED=1}", "`;globalThis.PWNED=1;//"]


def template_source(name):
    with open(os.path.join(TEMPLATES, name)) as fh:
        return fh.read()


class AttrCollector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.handlers, self.scripts, self._in_script, self._buf = [], [], False, []

    def handle_starttag(self, tag, attrs):
        for k, v in attrs:
            if k.startswith("on") and v is not None:
                self.handlers.append((tag, k, v))
        if tag == "script" and not dict(attrs).get("src"):
            self._in_script, self._buf = True, []

    def handle_endtag(self, tag):
        if tag == "script" and self._in_script:
            self._in_script = False
            self.scripts.append("".join(self._buf))

    def handle_data(self, data):
        if self._in_script:
            self._buf.append(data)


def collect(markup):
    c = AttrCollector()
    c.feed(markup)
    return c


def render(name, **ctx):
    with gym_app.app.test_request_context("/"):
        return gym_app.app.jinja_env.get_template(name).render(**ctx)


def run_node(code, timeout=20):
    return subprocess.run([NODE, "-e", code], capture_output=True, text=True, timeout=timeout)


STUBS = ("globalThis.PWNED = 0; globalThis.calls = [];"
         "for (const n of ['addExerciseToPlan','filterByCategory','openModal','openStats']) "
         "globalThis[n] = (...a) => globalThis.calls.push([n, a]);"
         "const location = {href: ''}; const window = {location}; globalThis.window = window;"
         "globalThis.encodeURIComponent = globalThis.encodeURIComponent;")


def execute_handlers(handlers):
    """Run every inline handler in node; return (PWNED flag, error output)."""
    script = STUBS + "const hs = " + json.dumps([h[2] for h in handlers]) + ";" \
        "for (const h of hs) { try { new Function('event','this_', h.replace(/\\bthis\\b/g, 'this_'))({}, {}); } " \
        "catch (e) { if (!(e instanceof ReferenceError)) throw e; } }" \
        "console.log(JSON.stringify({pwned: globalThis.PWNED, calls: globalThis.calls}));"
    out = run_node(script)
    if out.returncode:
        raise AssertionError(out.stderr)
    return json.loads(out.stdout.strip().splitlines()[-1])


@unittest.skipUnless(NODE, "node is required to execute rendered handlers")
class ServerRenderedHandlers(unittest.TestCase):
    def hostile(self):
        return PAYLOADS

    def test_create_page_exercise_rows_cannot_break_out(self):
        for payload in PAYLOADS:
            lib = [{"id": payload, "title": payload, "img": payload, "category_id": 1, "category_name": "c",
                    "device_type": 1, "device_type_tag": 1, "accessories": "", "variants": []}]
            page = render("create.html", library=lib, existing_workout=None, unit=0, custom_instruction=payload,
                          categories=[{"id": 1, "name": payload, "filter_ids": payload}], device_type=1,
                          allow_monster_moves=False)
            result = execute_handlers(collect(page).handlers)
            self.assertEqual(result["pwned"], 0, payload)
            added = [c for c in result["calls"] if c[0] == "addExerciseToPlan"]
            if added:  # the payload must arrive as DATA: the exact strings, in order
                self.assertEqual(added[0][1][1], payload)

    def test_create_page_custom_instruction_is_a_json_string(self):
        payload = "`;globalThis.PWNED=1;//${x}</script>"
        page = render("create.html", library=[], existing_workout=None, unit=0, custom_instruction=payload,
                      categories=[], device_type=1, allow_monster_moves=False)
        self.assertNotIn("</script><", page.replace("</script>\n", ""))  # the payload's </script> is escaped
        m = re.search(r"const customInstruction = (.*);", page)
        self.assertIsNotNone(m)
        self.assertEqual(json.loads(m.group(1).replace("\\u003c", "<").replace("\\u003e", ">")), payload)

    def test_library_rows_and_category_buttons_cannot_break_out(self):
        for payload in PAYLOADS:
            ex = [{"id": payload, "title": payload, "img": payload, "category_id": 1, "trainingPartId2": 1,
                   "device_type": 1, "device_type_tag": 1, "accessories": "", "equipment_name": payload,
                   "muscle_name": payload, "category_name": payload}]
            page = render("library.html", exercises=ex, categories=[{"id": payload, "name": payload, "filter_ids": payload}],
                          device_type=1, allow_monster_moves=False, owned_accessories=[], owned_devices=[])
            result = execute_handlers(collect(page).handlers)
            self.assertEqual(result["pwned"], 0, payload)

    def test_exercise_detail_modal_cannot_break_out(self):
        for payload in PAYLOADS:
            coach = {"name": payload, "avatar": payload}
            detail = {"title": payload, "context": payload, "steps": [{"img": payload, "context": payload}],
                      "actionLibraryList": [{"id": 1, "coach": coach, "videoPath": payload, "leftVideo": payload},
                                            {"id": 2, "coach": coach, "videoPath": payload, "leftVideo": payload}]}
            page = render("exercise_detail.html", ex=detail, accessories=[], ex_id=1, unit=0)
            result = execute_handlers(collect(page).handlers)
            self.assertEqual(result["pwned"], 0, payload)
            opened = [c for c in result["calls"] if c[0] == "openModal"]
            self.assertTrue(opened, "the step image handler must have run")


@unittest.skipUnless(NODE, "node is required")
class SharedHelpers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        layout = template_source("layout.html")
        m = re.search(r"<script>\s*(// Vendor-supplied.*?)</script>", layout, re.S)
        assert m, "esc()/jsq() helpers missing from layout.html"
        cls.helpers = m.group(1)

    def run_js(self, body):
        out = run_node(self.helpers + body)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.strip()

    def test_esc_leaves_no_markup_or_quote_characters(self):
        for payload in PAYLOADS + ["<b>", "a&b", "'", '"']:
            out = self.run_js(f"console.log(esc({json.dumps(payload)}))")
            for ch in "<>\"'":
                self.assertNotIn(ch, out, payload)

    def test_esc_handles_non_strings(self):
        self.assertEqual(self.run_js("console.log(esc(null)+'|'+esc(undefined)+'|'+esc(0)+'|'+esc(12.5))"), "||0|12.5")

    def test_jsq_survives_a_double_quoted_attribute_then_the_js_parser(self):
        for payload in PAYLOADS:
            code = ("globalThis.PWNED=0; function f(a){ globalThis.got=a }"
                    f"; const attr = 'f(' + jsq({json.dumps(payload)}) + ')';"
                    # an HTML parser decodes entities in the attribute BEFORE the JS engine sees it
                    "const decoded = attr.replace(/&quot;/g,'\"').replace(/&#39;/g,\"'\").replace(/&lt;/g,'<')"
                    ".replace(/&gt;/g,'>').replace(/&amp;/g,'&');"
                    "assertNoRawQuote = attr.includes('\"') === false;"
                    "new Function(decoded)();"
                    "console.log(JSON.stringify({pwned: globalThis.PWNED, got: globalThis.got, noQuote: assertNoRawQuote}))")
            out = json.loads(self.run_js(code).splitlines()[-1])
            self.assertEqual(out, {"pwned": 0, "got": payload, "noQuote": True}, payload)


class JsBuiltMarkupLint(unittest.TestCase):
    """The innerHTML builders cannot run without a browser; pin every previously vulnerable interpolation."""

    RAW_VENDOR_FIELD = re.compile(
        r"\$\{(?:ex|c|p|rec|item|v|x)\.(?:title|name|courseTitle|courseImg|coverImg|img|description|"
        r"actionLibraryName|mainMuscleGroupName|dayStr)\}")

    def test_no_raw_vendor_field_in_any_template_literal(self):
        offenders = []
        for name in sorted(os.listdir(TEMPLATES)):
            for n, line in enumerate(template_source(name).splitlines(), 1):
                if line.lstrip().startswith("prompt +="):  # plain-text LLM prompt, never parsed as HTML
                    continue
                if self.RAW_VENDOR_FIELD.search(line):
                    offenders.append(f"{name}:{n}: {line.strip()[:120]}")
        self.assertEqual(offenders, [])

    BARE_VENDOR_NAME = re.compile(r"\$\{(?:name|img|title|cats|label)\}")
    TEXT_ONLY_SINK = re.compile(r"textContent|innerText|fetch\(|\.title\s*=|document\.title")

    def test_no_raw_bare_identifier_in_html_building_literals(self):
        offenders = []
        for name in sorted(os.listdir(TEMPLATES)):
            for n, line in enumerate(template_source(name).splitlines(), 1):
                if self.BARE_VENDOR_NAME.search(line) and not self.TEXT_ONLY_SINK.search(line):
                    offenders.append(f"{name}:{n}: {line.strip()[:120]}")
        self.assertEqual(offenders, [])

    def test_every_image_src_interpolation_is_escaped(self):
        offenders = []
        for name in sorted(os.listdir(TEMPLATES)):
            for n, line in enumerate(template_source(name).splitlines(), 1):
                for m in re.finditer(r'src="\$\{([^}]*)\}"', line):
                    if not m.group(1).startswith("esc("):
                        offenders.append(f"{name}:{n}: {m.group(0)}")
        self.assertEqual(offenders, [])

    def test_no_js_string_interpolation_inside_inline_handlers(self):
        offenders = []
        for name in sorted(os.listdir(TEMPLATES)):
            for n, line in enumerate(template_source(name).splitlines(), 1):
                if re.search(r"""on\w+="[^"]*'\$\{""", line):
                    offenders.append(f"{name}:{n}")
        self.assertEqual(offenders, [], "use jsq(): a single-quoted ${} inside onclick is a JS-string breakout")

    def test_no_jinja_value_inside_a_quoted_js_string_in_a_handler(self):
        offenders = []
        for name in sorted(os.listdir(TEMPLATES)):
            for n, line in enumerate(template_source(name).splitlines(), 1):
                if re.search(r"""on\w+=["'][^"']*'\{\{[^}]*\}\}'""", line):
                    offenders.append(f"{name}:{n}")
        self.assertEqual(offenders, [], "Jinja HTML-escaping does not protect a JS string: use |tojson")

    def test_custom_instruction_is_never_marked_safe(self):
        self.assertNotRegex(template_source("create.html"), r"custom_instruction\s*\|\s*safe")

    def test_escaped_in_the_known_builders(self):
        expected = {
            "browse.html": ["esc(c.courseTitle)", "esc(ex.title)", "esc(p.name)", "esc(p.description)",
                            "jsq(name)", "jsq(c.courseTitle || '')", "esc(c.courseImg)", "esc(ex.img)"],
            "create.html": ["esc(ex.title)", "esc(ex.img)", "jsq(ex.title)", "esc(v.coach ? v.coach.name"],
            "history.html": ["esc(name)", "esc(rec.courseImg)", "esc(typeInfo.label)"],
            "workout_history.html": ["esc(item.title", "jsq(exportId)", "jsq(type)", "esc(workout.title"],
            "exercise_detail.html": ["esc(formatStatDate(item.dayStr))"],
        }
        for name, needles in expected.items():
            src = template_source(name)
            for needle in needles:
                self.assertIn(needle, src, f"{name}: {needle}")


@unittest.skipUnless(NODE, "node is required")
class InlineScriptsParse(unittest.TestCase):
    """Every rendered inline <script> still parses (an edit to a template literal can silently break a page)."""

    CASES = {
        "index.html": {"workouts": [], "unit": 0},
        "browse.html": {"unit": 0, "owned_accessories": [], "owned_devices": []},
        "history.html": {"unit": 0},
        "workout_history.html": {},
        "create.html": {"library": [], "existing_workout": None, "unit": 0, "custom_instruction": "x",
                        "categories": [], "device_type": 1, "allow_monster_moves": False},
        "library.html": {"exercises": [], "categories": [], "device_type": 1, "allow_monster_moves": False,
                         "owned_accessories": [], "owned_devices": []},
        "settings.html": {"creds": {"token_set": False, "region": "Global", "user_id": "", "unit": 0,
                                    "device_type": 1, "custom_instruction": "", "allow_monster_moves": False,
                                    "owned_accessories": [], "owned_devices": []}, "accessories": []},
    }

    def test_every_page_script_is_valid_javascript(self):
        for name, ctx in self.CASES.items():
            page = render(name, **ctx)
            scripts = collect(page).scripts
            self.assertTrue(scripts, name)
            for i, code in enumerate(scripts):
                with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
                    fh.write(code)
                try:
                    out = subprocess.run([NODE, "--check", fh.name], capture_output=True, text=True)
                finally:
                    os.unlink(fh.name)
                self.assertEqual(out.returncode, 0, f"{name} script #{i}: {out.stderr[:400]}")


if __name__ == "__main__":
    unittest.main()
