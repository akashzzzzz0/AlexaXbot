import unittest
from unittest.mock import AsyncMock, patch

from bot.services import ai_chat_provider


class AlexaChatProviderRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_groq_primary_succeeds_without_calling_gemini(self):
        groq = AsyncMock(return_value="Heyyy!")
        gemini = AsyncMock(return_value="Gemini reply")
        with (
            patch.object(ai_chat_provider, "_groq_api_key", return_value="groq-key"),
            patch.object(ai_chat_provider, "_gemini_api_key", return_value="gemini-key"),
            patch.object(ai_chat_provider, "_groq_chat", groq),
            patch.object(ai_chat_provider, "_gemini_chat", gemini),
        ):
            result = await ai_chat_provider.ai_chat_completion([{"role": "user", "content": "Hi"}])
        self.assertEqual(result, "Heyyy!")
        groq.assert_awaited_once()
        gemini.assert_not_awaited()

    async def test_gemini_is_used_when_groq_fails(self):
        groq = AsyncMock(side_effect=RuntimeError("Groq unavailable"))
        gemini = AsyncMock(return_value="Gemini reply")
        with (
            patch.object(ai_chat_provider, "_groq_api_key", return_value="groq-key"),
            patch.object(ai_chat_provider, "_gemini_api_key", return_value="gemini-key"),
            patch.object(ai_chat_provider, "_groq_chat", groq),
            patch.object(ai_chat_provider, "_gemini_chat", gemini),
        ):
            result = await ai_chat_provider.ai_chat_completion([{"role": "user", "content": "Hi"}])
        self.assertEqual(result, "Gemini reply")
        groq.assert_awaited_once()
        gemini.assert_awaited_once()

    async def test_returns_empty_when_both_free_providers_fail(self):
        groq = AsyncMock(side_effect=RuntimeError("Groq unavailable"))
        gemini = AsyncMock(side_effect=RuntimeError("Gemini unavailable"))
        with (
            patch.object(ai_chat_provider, "_groq_api_key", return_value="groq-key"),
            patch.object(ai_chat_provider, "_gemini_api_key", return_value="gemini-key"),
            patch.object(ai_chat_provider, "_groq_chat", groq),
            patch.object(ai_chat_provider, "_gemini_chat", gemini),
        ):
            result = await ai_chat_provider.ai_chat_completion([{"role": "user", "content": "Hi"}])
        self.assertEqual(result, "")
        groq.assert_awaited_once()
        gemini.assert_awaited_once()

    async def test_skips_unconfigured_groq_and_uses_gemini(self):
        groq = AsyncMock(return_value="should not be called")
        gemini = AsyncMock(return_value="Gemini reply")
        with (
            patch.object(ai_chat_provider, "_groq_api_key", return_value=""),
            patch.object(ai_chat_provider, "_gemini_api_key", return_value="gemini-key"),
            patch.object(ai_chat_provider, "_groq_chat", groq),
            patch.object(ai_chat_provider, "_gemini_chat", gemini),
        ):
            result = await ai_chat_provider.ai_chat_completion([{"role": "user", "content": "Hi"}])
        self.assertEqual(result, "Gemini reply")
        groq.assert_not_awaited()
        gemini.assert_awaited_once()

    async def test_never_calls_cloudflare_or_paid_provider_functions(self):
        groq = AsyncMock(return_value="Groq reply")
        cloudflare = AsyncMock(return_value="Cloudflare reply")
        openai = AsyncMock(return_value="Paid reply")
        with (
            patch.object(ai_chat_provider, "_groq_api_key", return_value="groq-key"),
            patch.object(ai_chat_provider, "_gemini_api_key", return_value="gemini-key"),
            patch.object(ai_chat_provider, "_groq_chat", groq),
            patch.object(ai_chat_provider, "_gemini_chat", AsyncMock()),
            patch.object(ai_chat_provider, "_cloudflare_chat", cloudflare),
            patch.object(ai_chat_provider, "_openai_chat", openai),
        ):
            result = await ai_chat_provider.ai_chat_completion([{"role": "user", "content": "Hi"}])
        self.assertEqual(result, "Groq reply")
        cloudflare.assert_not_awaited()
        openai.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
