"""Regression tests for message-first understanding in Alexa Own AI."""
import unittest
from unittest.mock import patch

import app
from model_runtime import _messages


class UnderstandingRoutingTests(unittest.TestCase):
    @patch("app.conversation_respond", return_value="I read that as playful laughter 😄")
    def test_emoji_only_message_reaches_model(self, generate):
        reply = app.response_for("😂", [{"role": "user", "content": "That was funny"}])
        self.assertEqual(reply, "I read that as playful laughter 😄")
        generate.assert_called_once_with("😂", "😂", "ur", [{"role": "user", "content": "That was funny"}])

    @patch("app.conversation_respond", return_value="You sound disappointed about what happened.")
    def test_open_ended_message_is_understood_before_keyword_rules(self, generate):
        reply = app.response_for("Yaar kal jo hua na, woh bilkul acha nahi laga.", [])
        self.assertEqual(reply, "You sound disappointed about what happened.")
        generate.assert_called_once()

    @patch("app.conversation_respond", return_value=None)
    def test_arithmetic_stays_on_exact_local_calculator(self, generate):
        reply = app.response_for("2 + 2", [])
        self.assertIn("4", reply)
        generate.assert_called_once()

    def test_model_receives_raw_emoji_and_recent_context(self):
        messages = _messages("🙂", [{"role": "user", "content": "Aaj mood acha hai"}])
        self.assertEqual(messages[-1]["content"], "🙂")
        self.assertEqual(messages[-2]["content"], "Aaj mood acha hai")
        system = messages[0]["content"].lower()
        self.assertIn("emoji", system)
        self.assertIn("context", system)


if __name__ == "__main__":
    unittest.main()
