"""Design tokens (static/ui-theme.css) and their wiring into the Tailwind config (templates/layout.html).

The WCAG 2.x contrast ratios are computed here from the token values, in both themes, for every text colour on every surface
it is used on. A token edit that makes a pair unreadable fails this file. (The browser check in docs/ui-theme.md measures the
rendered pages; this is the guard that keeps the tokens honest.)
"""
import json
import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")
CSS = open(os.path.join(ROOT, "static", "ui-theme.css"), encoding="utf-8").read()
LAYOUT = open(os.path.join(ROOT, "templates", "layout.html"), encoding="utf-8").read()
NODE = shutil.which("node")


def block_after(css, opener):
    """The body of the first rule that starts with `opener` (the rules here contain no nested braces)."""
    start = css.index(opener)
    body_start = css.index("{", start) + 1
    return css[body_start:css.index("}", body_start)]


def tokens(body):
    return {m.group(1): tuple(int(x) for x in m.group(2).split()) for m in re.finditer(r"--([a-z0-9-]+):\s*(\d+ \d+ \d+);", body)}


def media_block(css, header):
    start = css.index(header)
    depth, i = 0, css.index("{", start)
    begin = i + 1
    while True:
        depth += 1 if css[i] == "{" else -1 if css[i] == "}" else 0
        i += 1
        if depth == 0:
            return css[begin:i - 1]


DARK = tokens(block_after(CSS, ":root {"))
LIGHT = tokens(block_after(CSS, ':root[data-theme="light"] {'))
LIGHT_AUTO = tokens(block_after(media_block(CSS, "@media (prefers-color-scheme: light)"), ":root:not([data-theme])"))
LIGHT_PRINT = tokens(block_after(media_block(CSS, "@media print"), ":root, :root[data-theme]"))
THEMES = {"dark": DARK, "light": LIGHT}


def lum(rgb):
    def f(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (f(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ratio(a, b):
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def over(fg, alpha, bg):
    return tuple(round(f * alpha + b * (1 - alpha)) for f, b in zip(fg, bg))


SURFACES = ("canvas", "inset", "surface", "raised", "header-bg")
TEXT = ("fg", "fg-strong-base", "muted", "gray-300", "gray-400", "gray-500", "accent", "blue-400", "success", "warning", "danger",
        "red-400", "green-400", "yellow-400", "orange-400", "purple-400")
SOLIDS = [("blue", 500), ("blue", 600), ("blue", 700), ("green", 500), ("green", 600), ("green", 700), ("red", 600), ("red", 700),
          ("purple", 600), ("purple", 700), ("yellow", 600), ("yellow", 700), ("orange", 600), ("orange", 700)]


class TokenBlocks(unittest.TestCase):
    def test_the_light_theme_is_defined_for_an_explicit_choice_for_the_system_preference_and_for_print(self):
        self.assertTrue(LIGHT)
        self.assertEqual(LIGHT, LIGHT_AUTO)
        self.assertEqual(LIGHT, LIGHT_PRINT)

    def test_both_themes_define_exactly_the_same_colour_tokens(self):
        self.assertEqual(set(DARK), set(LIGHT))
        self.assertGreater(len(DARK), 80)

    def test_the_themes_differ(self):
        for name in ("canvas", "surface", "fg", "muted", "gray-800", "gray-300"):
            self.assertNotEqual(DARK[name], LIGHT[name], name)

    def test_dark_is_the_default_and_both_themes_set_color_scheme(self):
        self.assertIn("color-scheme: dark", block_after(CSS, ":root {"))
        self.assertIn("color-scheme: light", block_after(CSS, ':root[data-theme="light"] {'))
        self.assertIn("color-scheme: light", block_after(media_block(CSS, "@media (prefers-color-scheme: light)"), ":root:not([data-theme])"))

    def test_the_system_preference_only_applies_when_no_choice_was_stored(self):
        self.assertRegex(CSS, r"@media \(prefers-color-scheme: light\)\s*\{\s*:root:not\(\[data-theme\]\)")


class TailwindWiring(unittest.TestCase):
    @unittest.skipUnless(NODE, "node is required")
    def test_every_colour_the_tailwind_config_names_is_a_defined_token_in_both_themes(self):
        start, end = LAYOUT.index("// tailwind-config:start"), LAYOUT.index("// tailwind-config:end")
        code = "var tailwind = {};\n" + LAYOUT[start:end] + "\nconsole.log(JSON.stringify(tailwind.config));"
        out = subprocess.run([NODE, "-e", code], capture_output=True, text=True, timeout=20)
        self.assertEqual(out.returncode, 0, out.stderr)
        colors = json.loads(out.stdout)["theme"]["extend"]["colors"]
        used = set()

        def walk(node):
            if isinstance(node, dict):
                for v in node.values():
                    walk(v)
            else:
                self.assertRegex(node, r"^rgb\(var\(--[a-z0-9-]+\) / <alpha-value>\)$")  # alpha modifiers (bg-gray-800/50) must keep working
                used.add(re.search(r"--([a-z0-9-]+)", node).group(1))

        walk(colors)
        self.assertGreater(len(used), 70)
        self.assertIn("--fg-strong: var(--fg-strong-base);", CSS)  # text-white: derived, so a solid fill can override it locally
        used.discard("fg-strong")
        for name in sorted(used):
            self.assertIn(name, DARK, f"--{name} is not defined for the dark theme")
            self.assertIn(name, LIGHT, f"--{name} is not defined for the light theme")
        for semantic in ("surface", "raised", "muted", "line", "accent", "success", "warning", "danger", "canvas"):
            self.assertIn(semantic, colors)  # bg-surface, text-muted, ... are available to pages
        self.assertEqual(colors["white"], "rgb(var(--fg-strong) / <alpha-value>)")  # text-white follows the theme

    def test_the_solid_fill_exception_for_white_text_exists(self):
        self.assertRegex(CSS, r"\.bg-blue-600,[^{]*\{ --fg-strong: 255 255 255; \}")


class Contrast(unittest.TestCase):
    def check(self, theme, fg, bg, need, label):
        got = ratio(fg, bg)
        self.assertGreaterEqual(got, need, f"{theme}: {label} is {got:.2f}:1, needs {need}:1")

    def test_text_tokens_on_every_surface_meet_aa(self):
        for theme, t in THEMES.items():
            for fg in TEXT:
                for bg in SURFACES:
                    self.check(theme, t[fg], t[bg], 4.5, f"{fg} on {bg}")

    def test_white_text_on_solid_fills_meets_aa(self):
        for theme, t in THEMES.items():
            for hue, step in SOLIDS:
                self.check(theme, (255, 255, 255), t[f"{hue}-{step}"], 4.5, f"white on {hue}-{step}")
            self.check(theme, t["on-accent"], t["accent-solid"], 4.5, "on-accent on accent-solid")
            self.check(theme, t["on-accent"], t["accent-solid-hover"], 4.5, "on-accent on accent-solid-hover")

    def test_control_borders_and_focus_ring_meet_non_text_contrast(self):
        for theme, t in THEMES.items():
            for bg in ("inset", "surface", "canvas"):
                self.check(theme, t["control-line"], t[bg], 3.0, f"control border on {bg}")
                self.check(theme, t["focus"], t[bg], 3.0, f"focus ring on {bg}")

    def test_flash_messages_meet_aa(self):
        pairs = {"error": ("red-200", "red-900"), "success": ("green-200", "green-900"), "info": ("blue-200", "blue-900"),
                 "debug": ("fg", "surface")}
        for theme, t in THEMES.items():
            for kind, (fg, bg) in pairs.items():
                self.check(theme, t[fg], t[bg], 4.5, f"flash {kind}")

    def test_active_nav_link_and_chips_on_their_tinted_backgrounds_meet_aa(self):
        for theme, t in THEMES.items():
            nav_bg = over(t["accent"], 0.14, t["header-bg"])
            self.check(theme, t["blue-400"], nav_bg, 4.5, "active nav link on its tint")
            for fg, bg_token, alpha in (("blue-400", "accent", .16), ("success", "success", .16), ("warning", "warning", .16), ("danger", "danger", .16)):
                tint = over(t[bg_token], alpha, t["surface"])
                self.check(theme, t[fg], tint, 4.5, f"chip {bg_token} on its tint")

    def test_the_tokens_that_pages_use_as_card_borders_stay_visible_but_quiet(self):
        for theme, t in THEMES.items():
            self.assertGreater(ratio(t["line"], t["surface"]), 1.15, theme)  # a hairline, not invisible
            self.assertLess(ratio(t["line"], t["surface"]), 3.0, theme)     # decoration: loud borders belong to controls


class Behaviour(unittest.TestCase):
    def test_focus_is_visible_and_beats_tailwind_outline_none(self):
        self.assertRegex(CSS, r"html :is\([^)]*\):focus-visible\s*\{[^}]*outline:\s*2px solid rgb\(var\(--focus\)\)")

    def test_reduced_motion_is_respected(self):
        body = media_block(CSS, "@media (prefers-reduced-motion: reduce)")
        for needle in ("animation-duration", "transition-duration", "scroll-behavior"):
            self.assertIn(needle, body)

    def test_print_hides_the_chrome(self):
        body = media_block(CSS, "@media print")
        for needle in (".ui-header", ".ui-footer", ".ui-flash"):
            self.assertIn(needle, body)

    def test_the_menu_and_theme_button_need_script_so_the_page_works_without_it(self):
        self.assertRegex(CSS, r"\.ui-theme-btn \{ display: none; \}\s*\.js \.ui-theme-btn \{ display: inline-flex; \}")
        self.assertIn(".js .ui-nav[data-open=\"false\"] { display: none; }", CSS)  # without script the nav is simply always shown

    def test_legacy_hard_coded_hex_values_are_gone_from_the_page_styles(self):
        for name in ("index.html", "workout_history.html"):
            src = open(os.path.join(ROOT, "templates", name), encoding="utf-8").read()
            style = "".join(re.findall(r"<style>(.*?)</style>", src, re.S))
            self.assertNotRegex(style, r"#[0-9a-fA-F]{3,8}\b", name)  # white on a coloured pill is spelled #fff only where it is meant


if __name__ == "__main__":
    unittest.main()
