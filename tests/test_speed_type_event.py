import unittest
from datetime import datetime, timezone

from bot.handlers.world_events import (
    WINNER_REWARD,
    EVENT_INTERVAL_SECONDS,
    EVENT_DURATION_MINUTES,
    _event_caption,
    _normalize_text,
    _as_utc,
)


class SpeedTypeEventTests(unittest.TestCase):
    def test_normalization_ignores_case_and_extra_whitespace(self):
        self.assertEqual(
            _normalize_text("  Alexa   makes\n every group fun "),
            _normalize_text("alexa makes every group fun"),
        )

    def test_event_timing_and_winner_reward(self):
        self.assertEqual(WINNER_REWARD, 10)
        self.assertEqual(EVENT_INTERVAL_SECONDS, 3 * 60 * 60)
        self.assertEqual(EVENT_DURATION_MINUTES, 180)

    def test_naive_sqlite_event_datetime_is_normalized_to_utc(self):
        naive = datetime(2026, 11, 2, 5, 0, 0)
        normalized = _as_utc(naive)
        self.assertEqual(normalized.tzinfo, timezone.utc)
        self.assertEqual(normalized, datetime(2026, 11, 2, 5, 0, 0, tzinfo=timezone.utc))

    def test_aware_event_datetime_is_converted_to_utc(self):
        aware = datetime(2026, 11, 2, 10, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(_as_utc(aware), aware)

    def test_caption_uses_short_type_event_instructions(self):
        caption = _event_caption()
        self.assertIn("Type the exact word here", caption)
        self.assertIn("10 coins", caption)
        self.assertIn("First correct answer wins", caption)
        self.assertIn("3 hours", caption)


if __name__ == "__main__":
    unittest.main()
