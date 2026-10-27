"""The Settings page names the machine generation of each accessory: the vendor lists one entry per generation."""
import base64
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import test_round16  # noqa: F401  (sets the shared secret and imports the app the way the other page tests do)
from test_round16 import BASIC, client_with, gym_app


class SettingsAccessories(unittest.TestCase):
    def page(self, accessories):
        client = client_with(1, True)
        client.get_accessories = lambda: accessories
        with mock.patch.object(gym_app, "client", client):
            resp = gym_app.app.test_client().get("/settings", headers=BASIC)
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def test_each_duplicate_name_is_labelled_with_its_machine_generation(self):
        html = self.page([
            {"id": 2, "name": "Tricep Rope", "deviceCodes": "monster2s,monster2,monster1"},
            {"id": 882627794731008, "name": "Tricep Rope", "deviceCodes": "monster3s,monster3"},
            {"id": 882630752272384, "name": "Tricep Rope", "deviceCodes": "monsterultra"},
        ])
        self.assertEqual(html.count("Tricep Rope"), 3)
        self.assertIn("Monster 1 · Monster 2 · Monster 2s", html)
        self.assertIn("Monster 3 · Monster 3s", html)
        self.assertIn(">Ultra<", html)

    def test_an_accessory_without_codes_or_with_odd_values_still_renders_and_escapes(self):
        html = self.page([
            {"id": 1, "name": "Flat Bench"},
            {"id": 2, "name": "Barbell", "deviceCodes": None},
            {"id": 3, "name": "Rope", "deviceCodes": ["monster1"]},
            {"id": 4, "name": "Strap", "deviceCodes": "<script>alert(1)</script>"},
        ])
        self.assertIn("Flat Bench", html)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)


if __name__ == "__main__":
    unittest.main()
