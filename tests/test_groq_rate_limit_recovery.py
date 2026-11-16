import unittest
from types import SimpleNamespace

from bot.services import ai_chat_provider


class GroqRateLimitRecoveryTests(unittest.TestCase):
    def _error(self, headers, text=""):
        return SimpleNamespace(response=SimpleNamespace(headers=headers, text=text))

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

    def test_detects_organization_daily_token_quota(self):
        error = self._error({}, "Rate limit reached on tokens per day (TPD)")
        self.assertTrue(ai_chat_provider._is_groq_daily_token_quota_error(error))

    def test_daily_quota_without_reset_header_uses_long_cooldown(self):
        error = self._error({}, "tokens per day (TPD)")
        self.assertEqual(ai_chat_provider._groq_daily_quota_cooldown(error), 86400.0)

    def test_daily_quota_uses_provider_token_reset_header(self):
        error = self._error({
            "x-ratelimit-remaining-tokens": "0",
            "x-ratelimit-reset-tokens": "1d",
        }, "tokens per day (TPD)")
        self.assertEqual(ai_chat_provider._groq_daily_quota_cooldown(error), 86400.0)

    def test_organization_quota_cooldown_marks_every_key(self):
        from unittest.mock import patch

        ai_chat_provider._provider_retry_after.clear()
        ai_chat_provider._provider_rate_limited_until.clear()
        with patch.object(ai_chat_provider.time, "monotonic", return_value=100.0):
            ai_chat_provider._mark_groq_keys_rate_limited(3, 3600.0)
        for index in range(1, 4):
            name = f"groq_key_{index}"
            self.assertEqual(ai_chat_provider._provider_retry_after[name], 3700.0)
            self.assertEqual(ai_chat_provider._provider_rate_limited_until[name], 3700.0)

    def test_missing_headers_uses_safe_default(self):
        error = self._error({})
        self.assertEqual(
            ai_chat_provider._groq_rate_limit_cooldown(error),
            ai_chat_provider._PROVIDER_429_COOLDOWN,
        )


if __name__ == "__main__":
    unittest.main()
