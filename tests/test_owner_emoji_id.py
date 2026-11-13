import unittest
from types import SimpleNamespace

from bot.handlers.sticker_id import _extract_custom_emoji_ids


class CustomEmojiExtractionTests(unittest.TestCase):
    def test_extracts_custom_emoji_id_and_preview(self):
        entity = SimpleNamespace(
            type="custom_emoji", offset=1, length=2,
            custom_emoji_id="5368324170671202286",
        )
        self.assertEqual(
            _extract_custom_emoji_ids("A💎B", [entity]),
            [("💎", "5368324170671202286")],
        )

    def test_extracts_multiple_custom_emojis_and_ignores_regular_entities(self):
        entities = [
            SimpleNamespace(type="bold", offset=0, length=1, custom_emoji_id=None),
            SimpleNamespace(type="custom_emoji", offset=0, length=1, custom_emoji_id="111"),
            SimpleNamespace(type="custom_emoji", offset=1, length=2, custom_emoji_id="222"),
        ]
        self.assertEqual(
            _extract_custom_emoji_ids("X💎", entities),
            [("X", "111"), ("💎", "222")],
        )

    def test_empty_entities_return_no_matches(self):
        self.assertEqual(_extract_custom_emoji_ids("plain text", []), [])
        self.assertEqual(_extract_custom_emoji_ids("plain text", None), [])


if __name__ == "__main__":
    unittest.main()
