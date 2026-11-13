import unittest

from bot.handlers.owner_ai_control import _parse_model_json


class OwnerAIJSONParsingTests(unittest.TestCase):
    def test_parse_plain_json_object(self):
        self.assertEqual(_parse_model_json('{"action":"help","type":""}'), {
            "action": "help",
            "type": "",
        })

    def test_parse_json_fenced_by_markdown(self):
        raw = 'Here is the result:\n```json\n{"action":"help"}\n```'
        self.assertEqual(_parse_model_json(raw), {"action": "help"})

    def test_parse_json_with_leading_and_trailing_prose(self):
        self.assertEqual(
            _parse_model_json('Result: {"action":"list_groups"} done'),
            {"action": "list_groups"},
        )

    def test_reject_non_object_json(self):
        with self.assertRaises(ValueError):
            _parse_model_json('["help"]')

    def test_reject_text_without_json_object(self):
        with self.assertRaises(ValueError):
            _parse_model_json("I cannot classify this request")


if __name__ == "__main__":
    unittest.main()
