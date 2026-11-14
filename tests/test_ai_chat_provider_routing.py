import unittest
from unittest.mock import AsyncMock, patch

from bot.services import ai_chat_provider


class AlexaChatProviderRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_gemini_is_primary_and_stops_chain_on_success(self):
        gemini = AsyncMock(return_value="Gemini reply")
        grok = AsyncMock(return_value="Grok reply")
        with (
            patch.object(ai_chat_provider, "_gemini_api_key", return_value="gemini-key"),
            patch.object(ai_chat_provider, "_xai_api_key", return_value="xai-key"),
            patch.object(ai_chat_provider, "_openai_api_key", return_value="openai-key"),
            patch.object(ai_chat_provider, "_gemini_chat", gemini),
            patch.object(ai_chat_provider, "_xai_chat", grok),
        ):
            result = await ai_chat_provider.ai_chat_completion([{"role": "user", "content": "Hi"}])
        self.assertEqual(result, "Gemini reply")
        gemini.assert_awaited_once()
        grok.assert_not_awaited()

    async def test_uses_grok_after_gemini_failure(self):
        gemini = AsyncMock(side_effect=RuntimeError("Gemini unavailable"))
        grok = AsyncMock(return_value="Grok reply")
        openai = AsyncMock()
        with (
            patch.object(ai_chat_provider, "_gemini_api_key", return_value="gemini-key"),
            patch.object(ai_chat_provider, "_xai_api_key", return_value="xai-key"),
            patch.object(ai_chat_provider, "_openai_api_key", return_value="openai-key"),
            patch.object(ai_chat_provider, "_gemini_chat", gemini),
            patch.object(ai_chat_provider, "_xai_chat", grok),
            patch.object(ai_chat_provider, "_openai_chat", openai),
        ):
            result = await ai_chat_provider.ai_chat_completion([{"role": "user", "content": "Hi"}])
        self.assertEqual(result, "Grok reply")
        gemini.assert_awaited_once()
        grok.assert_awaited_once()
        openai.assert_not_awaited()

    async def test_uses_openai_after_gemini_and_grok_fail(self):
        gemini = AsyncMock(side_effect=RuntimeError("Gemini unavailable"))
        grok = AsyncMock(side_effect=RuntimeError("Grok unavailable"))
        openai = AsyncMock(return_value="OpenAI reply")
        with (
            patch.object(ai_chat_provider, "_gemini_api_key", return_value="gemini-key"),
            patch.object(ai_chat_provider, "_xai_api_key", return_value="xai-key"),
            patch.object(ai_chat_provider, "_openai_api_key", return_value="openai-key"),
            patch.object(ai_chat_provider, "_gemini_chat", gemini),
            patch.object(ai_chat_provider, "_xai_chat", grok),
            patch.object(ai_chat_provider, "_openai_chat", openai),
        ):
            result = await ai_chat_provider.ai_chat_completion([{"role": "user", "content": "Hi"}])
        self.assertEqual(result, "OpenAI reply")
        openai.assert_awaited_once()

    async def test_skips_unconfigured_providers(self):
        gemini = AsyncMock()
        grok = AsyncMock()
        openai = AsyncMock()
        with (
            patch.object(ai_chat_provider, "_gemini_api_key", return_value=""),
            patch.object(ai_chat_provider, "_xai_api_key", return_value=""),
            patch.object(ai_chat_provider, "_openai_api_key", return_value=""),
            patch.object(ai_chat_provider, "_gemini_chat", gemini),
            patch.object(ai_chat_provider, "_xai_chat", grok),
            patch.object(ai_chat_provider, "_openai_chat", openai),
        ):
            result = await ai_chat_provider.ai_chat_completion([{"role": "user", "content": "Hi"}])
        self.assertEqual(result, "")
        for provider in (gemini, grok, openai):
            provider.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
