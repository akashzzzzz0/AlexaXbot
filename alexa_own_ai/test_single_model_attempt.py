"""Ensure the bootstrap does not retry the same failed model request."""
import unittest
from unittest.mock import patch

import app
import bootstrap  # installs AI-first routing and disables duplicate model fallback


class SingleModelAttemptTests(unittest.TestCase):
    @patch("ai_router._conversation_respond", return_value=None)
    @patch("ai_router._model_generate", return_value=None)
    def test_open_message_attempts_hosted_model_only_once(self, model, _brain):
        reply = app.response_for("Can you explain why this happens?", [])
        model.assert_called_once()
        self.assertTrue(reply)


if __name__ == "__main__":
    unittest.main()
