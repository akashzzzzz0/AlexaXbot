import unittest
from datetime import datetime, timezone

from bot.handlers.world_events import (
    WINNER_REWARD,
    EVENT_INTERVAL_SECONDS,
    EVENT_DURATION_MINUTES,
    _event_caption,
    _normalize_text,
    _as_utc,
    CHALLENGES,
    SPEED_TYPE_REMINDER_DELAY_SECONDS,
    SPEED_TYPE_REMINDER_TEXT,
)


class SpeedTypeEventTests(unittest.TestCase):
    def test_normalization_ignores_case_and_extra_whitespace(self):
        self.assertEqual(
            _normalize_text("  Alexa   makes\n every group fun "),
            _normalize_text("alexa makes every group fun"),
        )

    def test_event_timing_and_winner_reward(self):
        self.assertEqual(WINNER_REWARD, 10)
        self.assertEqual(EVENT_INTERVAL_SECONDS, 4 * 60 * 60)
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
        self.assertEqual(
            caption,
            "Be the first to write the word\n"
            "Show in the photo to climb the mini game leaderboard",
        )

    def test_speed_type_reminder_is_sent_after_30_seconds_with_expected_copy(self):
        self.assertEqual(SPEED_TYPE_REMINDER_DELAY_SECONDS, 30)
        self.assertIn("HURRY UP! TIME IS RUNNING OUT!", SPEED_TYPE_REMINDER_TEXT)
        self.assertIn("The clock is ticking", SPEED_TYPE_REMINDER_TEXT)
        self.assertIn("claim the first place on the leaderboard", SPEED_TYPE_REMINDER_TEXT)

    def test_word_bank_has_500_unique_words(self):
        self.assertEqual(len(CHALLENGES), 500)
        self.assertEqual(len({word.casefold() for word in CHALLENGES}), 500)


if __name__ == "__main__":
    unittest.main()
