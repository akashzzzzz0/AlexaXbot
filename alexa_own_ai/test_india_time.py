"""Regression tests for deterministic India Standard Time replies."""
import datetime as dt
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

import app


class IndiaTimeTests(unittest.TestCase):
    def test_india_clock_is_explicitly_asia_kolkata(self):
        now = app.india_now()
        self.assertEqual(now.tzinfo.key, "Asia/Kolkata")
        self.assertEqual(now.utcoffset(), dt.timedelta(hours=5, minutes=30))

    @patch("app.conversation_respond", return_value=None)
    @patch("app.india_now", return_value=dt.datetime(2026, 11, 17, 20, 5, tzinfo=ZoneInfo("Asia/Kolkata")))
    def test_time_reply_uses_indian_standard_time(self, _clock, _brain):
        self.assertIn("08:05 PM IST", app.response_for("Abhi time kya hai?", []))

    @patch("app.conversation_respond", return_value=None)
    @patch("app.india_now", return_value=dt.datetime(2026, 11, 17, 20, 5, tzinfo=ZoneInfo("Asia/Kolkata")))
    def test_date_reply_uses_indian_calendar_date_and_weekday(self, _clock, _brain):
        self.assertIn("Tuesday, 17 November 2026", app.response_for("Aaj ki date kya hai?", []))


if __name__ == "__main__":
    unittest.main()
