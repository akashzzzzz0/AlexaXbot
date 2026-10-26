import unittest

from bot.handlers.world_events import (
    WINNER_REWARD,
    EVENT_INTERVAL_SECONDS,
    EVENT_DURATION_MINUTES,
    _event_caption,
    _normalize_text,
)


class SpeedTypeEventTests(unittest.TestCase):
    def test_normalization_ignores_case_and_extra_whitespace(self):
        self.assertEqual(
            _normalize_text("  Alexa   makes\n every group fun "),
            _normalize_text("alexa makes every group fun"),
        )

    def test_event_timing_and_winner_reward(self):
        self.assertEqual(WINNER_REWARD, 50)
        self.assertEqual(EVENT_INTERVAL_SECONDS, 3 * 60 * 60)
        self.assertEqual(EVENT_DURATION_MINUTES, 180)

    def test_caption_says_text_is_inside_spoiler_image(self):
        caption = _event_caption()
        self.assertIn("text written inside it", caption)
        self.assertIn("50 coins", caption)
        self.assertIn("First member", caption)


if __name__ == "__main__":
    unittest.main()
