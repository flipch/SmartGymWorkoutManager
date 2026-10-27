"""The shared page shell (templates/layout.html): navigation and its active state, landmarks, flash messages, the footer
credit, and the contract every page relies on (blocks `head` and `content`, helpers `esc` and `jsq`).

Pages are rendered inside a request context, so `request.path` is what the nav's active-link logic really sees.
"""
import base64
import os
import re
import sys
import tempfile
import unittest
from html.parser import HTMLParser

TOKEN = "t" * 32
os.environ["GYM_API_TOKEN"] = TOKEN
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="gym-test-data-")
os.environ.pop("GYM_AUTH_DISABLED", None)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from flask import flash  # noqa: E402

import app as gym_app  # noqa: E402

BASIC = {"Authorization": "Basic " + base64.b64encode(f"gym:{TOKEN}".encode()).decode()}
PAGE = '{% extends "layout.html" %}{% block title %}T{% endblock %}{% block head %}<meta name="x-head">{% endblock %}' \
       '{% block content %}<p id="body-marker">hello</p>{% endblock %}'

# (path of the page, the nav entry that must be marked as the current page)
ACTIVE = [
    ("/", "/"), ("/create", "/create"), ("/edit/ABC123", "/create"),
    ("/library", "/library"), ("/exercise/55", "/library"),
    ("/browse", "/browse"), ("/history", "/history"), ("/workout_history", "/history"),
    ("/insights", "/insights"), ("/coach", "/coach"), ("/settings", "/settings"),
]
# Paths that look like a nav entry but are not one: nothing may be marked current.
NOT_ACTIVE = ["/nope", "/created", "/editor", "/libraryx", "/exercises", "/settings/unit", "/api/insights", "/history/old"]


class Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags, self.links, self.current, self.flashes, self.ids = [], [], [], [], {}
        self.in_footer = False
        self.footer_links = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        self.tags.append((tag, a))
        if a.get("id"):
            self.ids[a["id"]] = tag
        if tag == "footer":
            self.in_footer = True
        if tag == "a":
            self.links.append(a)
            if a.get("aria-current") == "page":
                self.current.append(a.get("href"))
            if self.in_footer:
                self.footer_links.append(a)
        if "data-ui-flash" in a:
            self.flashes.append({"role": a.get("role"), "class": a.get("class", "")})

    def handle_endtag(self, tag):
        if tag == "footer":
            self.in_footer = False


def parse(html):
    p = Page()
    p.feed(html)
    return p


def render_page(path="/", source=PAGE, flashes=()):
    with gym_app.app.test_request_context(path):
        for category, message in flashes:
            flash(message, category)
        return gym_app.app.jinja_env.from_string(source).render()


class Navigation(unittest.TestCase):
    def test_every_nav_entry_is_marked_current_on_its_own_pages(self):
        for path, href in ACTIVE:
            page = parse(render_page(path))
            self.assertEqual(page.current, [href], path)

    def test_lookalike_paths_mark_nothing(self):
        for path in NOT_ACTIVE:
            self.assertEqual(parse(render_page(path)).current, [], path)

    def test_nav_lists_every_destination_once_in_four_groups(self):
        page = parse(render_page("/"))
        nav_hrefs = [a["href"] for a in page.links if a.get("class") == "ui-nav-link"]
        self.assertEqual(nav_hrefs, ["/", "/create", "/library", "/browse", "/history", "/insights", "/coach", "/settings"])
        html = render_page("/")
        labels = re.findall(r'class="ui-nav-label"[^>]*>([^<]+)<', html)
        self.assertEqual(labels, ["Train", "Progress", "Coach", "Account"])

    def test_real_pages_render_inside_the_shell_with_the_right_current_link(self):
        client = gym_app.app.test_client()
        for path, href in [("/insights", "/insights")]:
            resp = client.get(path, headers=BASIC)
            self.assertEqual(resp.status_code, 200, path)
            self.assertEqual(parse(resp.get_data(as_text=True)).current, [href], path)


class Landmarks(unittest.TestCase):
    def setUp(self):
        self.html = render_page("/library")
        self.page = parse(self.html)

    def test_landmarks_and_skip_link(self):
        tags = [t for t, _ in self.page.tags]
        for landmark in ("header", "nav", "main", "footer"):
            self.assertEqual(tags.count(landmark), 1, landmark)
        skip = [a for a in self.page.links if a.get("class") == "ui-skip"]
        self.assertEqual([a["href"] for a in skip], ["#main-content"])
        self.assertEqual(self.page.ids.get("main-content"), "main")
        self.assertIn('aria-label="Main"', self.html)
        # the skip link is the first thing in the page, before the header
        self.assertLess(self.html.index("ui-skip"), self.html.index("<header"))

    def test_blocks_head_and_content_are_honoured(self):
        self.assertIn('<meta name="x-head">', self.html)
        self.assertIn('id="body-marker"', self.html)
        self.assertLess(self.html.index('id="main-content"'), self.html.index('id="body-marker"'))
        self.assertLess(self.html.index('id="body-marker"'), self.html.index("<footer"))

    def test_theme_and_menu_controls_are_labelled_buttons(self):
        buttons = {a.get("id"): a for t, a in self.page.tags if t == "button"}
        self.assertEqual(buttons["ui-menu-btn"].get("aria-expanded"), "false")
        self.assertEqual(buttons["ui-menu-btn"].get("aria-controls"), "ui-nav")
        self.assertIn("Theme:", buttons["ui-theme-btn"].get("aria-label", ""))
        self.assertEqual(buttons["ui-theme-btn"].get("type"), "button")

    def test_theme_is_set_before_the_stylesheet_so_nothing_flashes(self):
        self.assertLess(self.html.index("ui-theme:start"), self.html.index('href="/static/ui-theme.css"'))
        self.assertLess(self.html.index('href="/static/ui-theme.css"'), self.html.index("cdn.tailwindcss.com"))
        self.assertLess(self.html.index("tailwind-config:start"), self.html.index("function esc"))


class Footer(unittest.TestCase):
    def test_donation_credit_moved_from_the_top_banner_to_the_footer(self):
        html = render_page("/")
        body_before_footer = html[:html.index("<footer")]
        self.assertNotIn("buymeacoffee", body_before_footer)
        self.assertNotIn("Buy me a coffee", body_before_footer)
        footer = parse(html).footer_links
        self.assertEqual([a["href"] for a in footer], ["https://buymeacoffee.com/hbui3"])
        self.assertEqual(footer[0].get("target"), "_blank")
        self.assertEqual(set(footer[0].get("rel", "").split()), {"noopener", "noreferrer"})
        self.assertIn("upstream author", html)


class FlashMessages(unittest.TestCase):
    def flashes(self, items):
        return parse(render_page("/", flashes=items)).flashes

    def test_each_category_has_its_own_style_and_a_live_region_role(self):
        got = self.flashes([("error", "e"), ("success", "s"), ("info", "i"), ("debug", "d"), ("anything-else", "x")])
        self.assertEqual([f["role"] for f in got], ["alert", "status", "status", "status", "status"])
        self.assertEqual([re.search(r"ui-flash-(\w+)", f["class"]).group(1) for f in got],
                         ["error", "success", "info", "debug", "info"])

    def test_messages_are_escaped_not_injected(self):
        hostile = '<script>globalThis.PWNED=1</script><img src=x onerror=alert(1)>"\''
        html = render_page("/", flashes=[("error", hostile), ("success", hostile), ("debug", hostile), ("info", hostile)])
        self.assertNotIn("<script>globalThis.PWNED", html)
        self.assertNotIn("<img src=x", html)
        self.assertEqual(html.count("&lt;script&gt;globalThis.PWNED=1&lt;/script&gt;"), 4)

    def test_a_category_cannot_inject_markup_into_the_class_attribute(self):
        html = render_page("/", flashes=[('x" onmouseover="alert(1)', "m")])
        self.assertNotIn("onmouseover", html)
        self.assertIn("ui-flash-info", html)

    def test_every_message_can_be_dismissed(self):
        html = render_page("/", flashes=[("error", "a"), ("info", "b")])
        self.assertEqual(html.count('class="ui-flash-close"'), 2)
        self.assertEqual(html.count('aria-label="Dismiss message"'), 2)
        self.assertEqual(html.count('onclick="uiDismissFlash(this)"'), 2)

    def test_debug_messages_keep_their_label(self):
        self.assertIn("<strong>Debug Info:</strong>", render_page("/", flashes=[("debug", "trace")]))

    def test_no_messages_render_no_flash_markup(self):
        self.assertNotIn('class="ui-flash ', render_page("/"))


class SharedHelpers(unittest.TestCase):
    def test_esc_and_jsq_are_defined_once_in_the_layout_head(self):
        html = render_page("/")
        self.assertEqual(len(re.findall(r"function esc\(v\)", html)), 1)
        self.assertEqual(len(re.findall(r"function jsq\(v\)", html)), 1)


if __name__ == "__main__":
    unittest.main()
