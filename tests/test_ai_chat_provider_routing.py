import unittest
from unittest.mock import AsyncMock, patch

from bot.services import ai_chat_provider


class AlexaChatProviderRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_cloudflare_primary_succeeds_without_calling_gemini(self):
        cloudflare = AsyncMock(return_value="Heyyy!")
        gemini = AsyncMock(return_value="Gemini reply")
        with (
            patch.object(ai_chat_provider, "_cloudflare_account_id", return_value="account"),
            patch.object(ai_chat_provider, "_cloudflare_api_token", return_value="token"),
            patch.object(ai_chat_provider, "_gemini_api_key", return_value="gemini-key"),
            patch.object(ai_chat_provider, "_cloudflare_chat", cloudflare),
            patch.object(ai_chat_provider, "_gemini_chat", gemini),
        ):
            result = await ai_chat_provider.ai_chat_completion([{"role": "user", "content": "Hi"}])
        self.assertEqual(result, "Heyyy!")
        cloudflare.assert_awaited_once()
        gemini.assert_not_awaited()

    async def test_gemini_is_the_only_fallback_when_cloudflare_fails(self):
        cloudflare = AsyncMock(side_effect=RuntimeError("Cloudflare unavailable"))
        gemini = AsyncMock(return_value="Gemini reply")
        with (
            patch.object(ai_chat_provider, "_cloudflare_account_id", return_value="account"),
            patch.object(ai_chat_provider, "_cloudflare_api_token", return_value="token"),
            patch.object(ai_chat_provider, "_gemini_api_key", return_value="gemini-key"),
            patch.object(ai_chat_provider, "_cloudflare_chat", cloudflare),
            patch.object(ai_chat_provider, "_gemini_chat", gemini),
        ):
            result = await ai_chat_provider.ai_chat_completion([{"role": "user", "content": "Hi"}])
        self.assertEqual(result, "Gemini reply")
        cloudflare.assert_awaited_once()
        gemini.assert_awaited_once()

    async def test_returns_empty_string_when_both_providers_fail(self):
        cloudflare = AsyncMock(side_effect=RuntimeError("Cloudflare unavailable"))
        gemini = AsyncMock(side_effect=RuntimeError("Gemini unavailable"))
        with (
            patch.object(ai_chat_provider, "_cloudflare_account_id", return_value="account"),
            patch.object(ai_chat_provider, "_cloudflare_api_token", return_value="token"),
            patch.object(ai_chat_provider, "_gemini_api_key", return_value="gemini-key"),
            patch.object(ai_chat_provider, "_cloudflare_chat", cloudflare),
            patch.object(ai_chat_provider, "_gemini_chat", gemini),
        ):
            result = await ai_chat_provider.ai_chat_completion([{"role": "user", "content": "Hi"}])
        self.assertEqual(result, "")
        cloudflare.assert_awaited_once()
        gemini.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
