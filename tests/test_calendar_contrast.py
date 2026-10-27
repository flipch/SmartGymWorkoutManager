"""The history calendar's days from the neighbouring months stay readable: quieter by background, never by opacity.

Measured in a browser before the fix: opacity .3 on the cell and text-gray-600 gave their numbers 1.58:1 (light) and 1.81:1 (dark).
"""
import os
import re
import unittest

TEMPLATE = os.path.join(os.path.dirname(__file__), "..", "templates", "workout_history.html")


class CalendarContrast(unittest.TestCase):
    def setUp(self):
        self.src = open(TEMPLATE).read()

    def test_no_calendar_cell_is_faded_with_opacity(self):
        self.assertNotIn("opacity-30", self.src)
        self.assertEqual(self.src.count("cell.classList.add('outside-month')"), 2)  # the previous and the next month

    def test_the_outside_month_style_does_not_use_opacity(self):
        rule = re.search(r"\.history-calendar-day\.outside-month\s*\{([^}]*)\}", self.src)
        self.assertIsNotNone(rule)
        self.assertNotIn("opacity", rule.group(1))

    def test_the_outside_month_number_is_not_the_darkest_gray(self):
        self.assertNotIn("text-gray-600 font-bold mb-1", self.src)
        self.assertIn("'text-gray-300 font-bold mb-1' : 'text-gray-400 font-bold mb-1'", self.src)


if __name__ == "__main__":
    unittest.main()
