import unittest
from types import SimpleNamespace

from bot.services import ai_chat_provider


class GroqRateLimitRecoveryTests(unittest.TestCase):
    def _error(self, headers):
        return SimpleNamespace(response=SimpleNamespace(headers=headers))

    def test_retry_after_seconds_is_used(self):
        error = self._error({"retry-after": "45"})
        self.assertEqual(ai_chat_provider._groq_rate_limit_cooldown(error), 45.0)

    def test_retry_after_duration_is_used(self):
        error = self._error({"retry-after": "2m3.5s"})
        self.assertEqual(ai_chat_provider._groq_rate_limit_cooldown(error), 123.5)

    def test_exhausted_request_limit_uses_its_reset_header(self):
        error = self._error({
            "x-ratelimit-remaining-requests": "0",
            "x-ratelimit-reset-requests": "1h",
            "x-ratelimit-remaining-tokens": "100",
            "x-ratelimit-reset-tokens": "5s",
        })
        self.assertEqual(ai_chat_provider._groq_rate_limit_cooldown(error), 3600.0)

    def test_missing_headers_uses_safe_default(self):
        error = self._error({})
        self.assertEqual(
            ai_chat_provider._groq_rate_limit_cooldown(error),
            ai_chat_provider._PROVIDER_429_COOLDOWN,
        )


if __name__ == "__main__":
    unittest.main()
