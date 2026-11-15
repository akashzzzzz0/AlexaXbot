import unittest
from unittest.mock import AsyncMock, patch

from bot.services import ai_chat_provider


class AlexaChatProviderRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_grok_is_primary_and_stops_chain_on_success(self):
        grok = AsyncMock(return_value="Grok reply")
        openai = AsyncMock(return_value="OpenAI reply")
        with (
            patch.object(ai_chat_provider, "_xai_api_key", return_value="xai-key"),
            patch.object(ai_chat_provider, "_openai_api_key", return_value="openai-key"),
            patch.object(ai_chat_provider, "_xai_chat", grok),
            patch.object(ai_chat_provider, "_openai_chat", openai),
            patch.object(ai_chat_provider, "_gemini_api_key", return_value="gemini-key"),
            patch.object(ai_chat_provider, "_gemini_chat", AsyncMock()) as gemini,
        ):
            result = await ai_chat_provider.ai_chat_completion([{"role": "user", "content": "Hi"}])
        self.assertEqual(result, "Grok reply")
        grok.assert_awaited_once()
        openai.assert_not_awaited()
        gemini.assert_not_awaited()

    async def test_uses_openai_when_grok_fails(self):
        grok = AsyncMock(side_effect=RuntimeError("Grok unavailable"))
        openai = AsyncMock(return_value="OpenAI reply")
        with (
            patch.object(ai_chat_provider, "_xai_api_key", return_value="xai-key"),
            patch.object(ai_chat_provider, "_openai_api_key", return_value="openai-key"),
            patch.object(ai_chat_provider, "_xai_chat", grok),
            patch.object(ai_chat_provider, "_openai_chat", openai),
        ):
            result = await ai_chat_provider.ai_chat_completion([{"role": "user", "content": "Hi"}])
        self.assertEqual(result, "OpenAI reply")
        grok.assert_awaited_once()
        openai.assert_awaited_once()

    async def test_skips_unconfigured_chat_providers(self):
        grok = AsyncMock()
        openai = AsyncMock()
        with (
            patch.object(ai_chat_provider, "_xai_api_key", return_value=""),
            patch.object(ai_chat_provider, "_openai_api_key", return_value=""),
            patch.object(ai_chat_provider, "_xai_chat", grok),
            patch.object(ai_chat_provider, "_openai_chat", openai),
        ):
            result = await ai_chat_provider.ai_chat_completion([{"role": "user", "content": "Hi"}])
        self.assertEqual(result, "")
        grok.assert_not_awaited()
        openai.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
