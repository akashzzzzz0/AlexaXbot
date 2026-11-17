"""Regression tests for AI-first conversation routing."""
import unittest
from unittest.mock import patch

import ai_router


class RouterTests(unittest.TestCase):
    def test_known_conversation_reply_stays_local(self):
        with patch.object(ai_router, "_conversation_respond", return_value="Local greeting") as brain, patch.object(
            ai_router, "_model_generate"
        ) as model:
            self.assertEqual(ai_router.route_response("hi", "hi", "en", []), "Local greeting")
        brain.assert_called_once()
        model.assert_not_called()

    def test_unknown_english_message_reaches_model(self):
        with patch.object(ai_router, "_conversation_respond", return_value=None), patch.object(
            ai_router, "_model_generate", return_value="A natural answer."
        ) as model:
            self.assertEqual(
                ai_router.route_response("Explain why the sky changes color.", "explain why the sky changes color", "en", []),
                "A natural answer.",
            )
        model.assert_called_once()

    def test_open_roman_urdu_message_reaches_model(self):
        with patch.object(ai_router, "_conversation_respond", return_value=None), patch.object(
            ai_router, "_model_generate", return_value="Iski wajah roshni ka bikharna hai."
        ) as model:
            self.assertTrue(ai_router.route_response("Aisa kyun hota hai zara samjhao", "aisa kyun hota hai zara samjhao", "ur", []))
        model.assert_called_once()

    def test_math_stays_with_local_handler(self):
        with patch.object(ai_router, "_conversation_respond", return_value=None), patch.object(
            ai_router, "_model_generate"
        ) as model:
            self.assertIsNone(ai_router.route_response("2 + 2", "2 + 2", "en", []))
        model.assert_not_called()

    def test_model_failure_falls_through_to_existing_app_fallback(self):
        with patch.object(ai_router, "_conversation_respond", return_value=None), patch.object(
            ai_router, "_model_generate", return_value=None
        ):
            self.assertIsNone(ai_router.route_response("Some unusual thought", "some unusual thought", "en", []))


if __name__ == "__main__":
    unittest.main()
