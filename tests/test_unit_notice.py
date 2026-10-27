"""The "unit not set" notice: after the skip link and the nav, inside <main>, themed, announced, and never silent."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import test_round16  # noqa: F401  (sets the shared secret and imports the app the way the other page tests do)
from test_round16 import BASIC, client_with, gym_app


def page(path, unit_confirmed):
    client = client_with(0 if not unit_confirmed else 1, unit_confirmed)
    client.get_user_workouts = lambda: []
    client.get_accessories = lambda: []
    with mock.patch.object(gym_app, "client", client):
        return gym_app.app.test_client().get(path, headers=BASIC).get_data(as_text=True)


class UnitNotice(unittest.TestCase):
    def test_it_comes_after_the_skip_link_and_the_nav_and_inside_main(self):
        html = page("/", unit_confirmed=False)
        skip, header, main, notice = (html.index(x) for x in ('class="ui-skip"', "<header", "<main", "data-ui-unit-notice"))
        self.assertLess(skip, header)   # the skip link is the first focusable thing on the page
        self.assertLess(header, main)
        self.assertLess(main, notice)   # the skip link lands on <main>, which starts with the notice
        self.assertLess(notice, html.index("</main>"))

    def test_it_is_themed_and_announced_and_appears_once(self):
        html = page("/", unit_confirmed=False)
        self.assertEqual(html.count("has not been set"), 1)
        self.assertIn('class="ui-flash ui-flash-warn" role="status" data-ui-unit-notice', html)
        self.assertNotIn("background:#92400e", html)  # no hard-coded colours: the theme owns them in both modes
        self.assertIn('<a href="/settings">Choose it in Settings</a>', html)
        self.assertNotIn("<!--ui-unit-notice-->", html)

    def test_it_is_absent_once_the_unit_is_confirmed_and_on_the_settings_page(self):
        self.assertNotIn("has not been set", page("/", unit_confirmed=True))
        self.assertNotIn("has not been set, so weights", page("/settings", unit_confirmed=False))

    def test_a_page_without_the_marker_still_gets_the_warning_after_body(self):
        client = client_with(0, False)
        with mock.patch.object(gym_app, "client", client), gym_app.app.test_request_context("/somewhere"):
            response = gym_app.app.response_class("<html><body class='x'><p>bare page</p></body></html>", mimetype="text/html")
            out = gym_app.warn_when_the_unit_is_unset(response).get_data(as_text=True)
        self.assertIn("has not been set", out)
        self.assertLess(out.index("<body"), out.index("has not been set"))
        self.assertLess(out.index("has not been set"), out.index("bare page"))

    def test_the_theme_defines_the_warn_style_and_the_layout_carries_the_marker(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        css = open(os.path.join(root, "static", "ui-theme.css")).read()
        layout = open(os.path.join(root, "templates", "layout.html")).read()
        self.assertIn(".ui-flash-warn", css)
        self.assertEqual(layout.count("<!--ui-unit-notice-->"), 1)
        self.assertLess(layout.index('<main id="main-content"'), layout.index("<!--ui-unit-notice-->"))


if __name__ == "__main__":
    unittest.main()
