import unittest
from unittest.mock import AsyncMock, patch

from bot.services import ai_chat_provider


class AlexaChatProviderRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_groq_is_primary_and_stops_chain_on_success(self):
        groq = AsyncMock(return_value="Groq reply")
        grok = AsyncMock(return_value="Grok reply")
        openai = AsyncMock(return_value="OpenAI reply")
        gemini = AsyncMock(return_value="Gemini reply")
        with (
            patch.object(ai_chat_provider, "_groq_api_key", return_value="groq-key"),
            patch.object(ai_chat_provider, "_xai_api_key", return_value="xai-key"),
            patch.object(ai_chat_provider, "_openai_api_key", return_value="openai-key"),
            patch.object(ai_chat_provider, "_groq_chat", groq),
            patch.object(ai_chat_provider, "_xai_chat", grok),
            patch.object(ai_chat_provider, "_openai_chat", openai),
            patch.object(ai_chat_provider, "_gemini_api_key", return_value="gemini-key"),
            patch.object(ai_chat_provider, "_gemini_chat", AsyncMock()) as gemini,
        ):
            result = await ai_chat_provider.ai_chat_completion([{"role": "user", "content": "Hi"}])
        self.assertEqual(result, "Groq reply")
        groq.assert_awaited_once()
        grok.assert_not_awaited()
        openai.assert_not_awaited()
        gemini.assert_not_awaited()

    async def test_falls_back_to_grok_when_groq_fails(self):
        groq = AsyncMock(side_effect=RuntimeError("Groq unavailable"))
        grok = AsyncMock(return_value="Grok reply")
        openai = AsyncMock(return_value="OpenAI reply")
        with (
            patch.object(ai_chat_provider, "_groq_api_key", return_value="groq-key"),
            patch.object(ai_chat_provider, "_xai_api_key", return_value="xai-key"),
            patch.object(ai_chat_provider, "_openai_api_key", return_value="openai-key"),
            patch.object(ai_chat_provider, "_groq_chat", groq),
            patch.object(ai_chat_provider, "_xai_chat", grok),
            patch.object(ai_chat_provider, "_openai_chat", openai),
        ):
            result = await ai_chat_provider.ai_chat_completion([{"role": "user", "content": "Hi"}])
        self.assertEqual(result, "Grok reply")
        groq.assert_awaited_once()
        grok.assert_awaited_once()
        openai.assert_not_awaited()

    async def test_falls_back_to_gemini_when_groq_and_grok_fail(self):
        groq = AsyncMock(side_effect=RuntimeError("Groq unavailable"))
        grok = AsyncMock(side_effect=RuntimeError("Grok unavailable"))
        openai = AsyncMock(return_value="OpenAI reply")
        with (
            patch.object(ai_chat_provider, "_groq_api_key", return_value="groq-key"),
            patch.object(ai_chat_provider, "_xai_api_key", return_value="xai-key"),
            patch.object(ai_chat_provider, "_openai_api_key", return_value="openai-key"),
            patch.object(ai_chat_provider, "_gemini_api_key", return_value="gemini-key"),
            patch.object(ai_chat_provider, "_groq_chat", groq),
            patch.object(ai_chat_provider, "_xai_chat", grok),
            patch.object(ai_chat_provider, "_openai_chat", openai),
            patch.object(ai_chat_provider, "_gemini_chat", gemini),
        ):
            result = await ai_chat_provider.ai_chat_completion([{"role": "user", "content": "Hi"}])
        self.assertEqual(result, "Gemini reply")
        groq.assert_awaited_once()
        grok.assert_awaited_once()
        openai.assert_not_awaited()
        gemini.assert_awaited_once()

    async def test_skips_unconfigured_chat_providers(self):
        groq = AsyncMock()
        grok = AsyncMock()
        openai = AsyncMock()
        with (
            patch.object(ai_chat_provider, "_groq_api_key", return_value=""),
            patch.object(ai_chat_provider, "_xai_api_key", return_value=""),
            patch.object(ai_chat_provider, "_openai_api_key", return_value=""),
            patch.object(ai_chat_provider, "_groq_chat", groq),
            patch.object(ai_chat_provider, "_xai_chat", grok),
            patch.object(ai_chat_provider, "_openai_chat", openai),
        ):
            result = await ai_chat_provider.ai_chat_completion([{"role": "user", "content": "Hi"}])
        self.assertEqual(result, "")
        groq.assert_not_awaited()
        grok.assert_not_awaited()
        openai.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
