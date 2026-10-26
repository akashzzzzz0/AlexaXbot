import unittest

from bot.handlers.world_events import (
    FIRST_PLACE_REWARD,
    SECOND_PLACE_REWARD,
    THIRD_PLACE_REWARD,
    STREAK_BONUS,
    _event_caption,
    _normalize_text,
)


class SpeedTypeEventTests(unittest.TestCase):
    def test_normalization_ignores_case_and_extra_whitespace(self):
        self.assertEqual(
            _normalize_text("  Alexa   makes\n every group fun! "),
            _normalize_text("alexa makes every group fun!"),
        )

    def test_reward_tiers_match_approved_values(self):
        self.assertEqual((FIRST_PLACE_REWARD, SECOND_PLACE_REWARD, THIRD_PLACE_REWARD), (500, 250, 100))
        self.assertEqual(STREAK_BONUS, 200)

    def test_caption_hides_prompt_and_lists_all_reward_places(self):
        caption = _event_caption("Alexa makes every group fun!")
        self.assertIn("<tg-spoiler>Alexa makes every group fun!</tg-spoiler>", caption)
        self.assertIn("500", caption)
        self.assertIn("250", caption)
        self.assertIn("100", caption)
        self.assertIn("200", caption)


if __name__ == "__main__":
    unittest.main()
