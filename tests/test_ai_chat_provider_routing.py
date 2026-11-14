import unittest
from unittest.mock import AsyncMock, patch

from bot.services import ai_chat_provider


class AlexaChatProviderRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_groq_is_primary_and_openai_is_not_called_when_groq_succeeds(self):
        groq = AsyncMock(return_value="Hiii 💗")
        openai = AsyncMock(return_value="OpenAI reply")

        with (
            patch.object(ai_chat_provider, "_groq_api_key", return_value="groq-key"),
            patch.object(ai_chat_provider, "_openai_api_key", return_value="openai-key"),
            patch.object(ai_chat_provider, "_groq_chat", groq),
            patch.object(ai_chat_provider, "_openai_chat", openai),
        ):
            result = await ai_chat_provider.ai_chat_completion(
                [{"role": "user", "content": "Hi"}]
            )

        self.assertEqual(result, "Hiii 💗")
        groq.assert_awaited_once()
        openai.assert_not_awaited()

    async def test_openai_is_the_only_fallback_after_groq_fails(self):
        groq = AsyncMock(side_effect=RuntimeError("Groq unavailable"))
        openai = AsyncMock(return_value="Heyyy!")

        with (
            patch.object(ai_chat_provider, "_groq_api_key", return_value="groq-key"),
            patch.object(ai_chat_provider, "_openai_api_key", return_value="openai-key"),
            patch.object(ai_chat_provider, "_groq_chat", groq),
            patch.object(ai_chat_provider, "_openai_chat", openai),
        ):
            result = await ai_chat_provider.ai_chat_completion(
                [{"role": "user", "content": "Hey"}]
            )

        self.assertEqual(result, "Heyyy!")
        groq.assert_awaited_once()
        openai.assert_awaited_once()

    async def test_openai_is_used_when_groq_is_not_configured(self):
        groq = AsyncMock()
        openai = AsyncMock(return_value="Hello!")

        with (
            patch.object(ai_chat_provider, "_groq_api_key", return_value=""),
            patch.object(ai_chat_provider, "_openai_api_key", return_value="openai-key"),
            patch.object(ai_chat_provider, "_groq_chat", groq),
            patch.object(ai_chat_provider, "_openai_chat", openai),
        ):
            result = await ai_chat_provider.ai_chat_completion(
                [{"role": "user", "content": "Hello"}]
            )

        self.assertEqual(result, "Hello!")
        groq.assert_not_awaited()
        openai.assert_awaited_once()

    async def test_cloudflare_is_primary_and_groq_is_not_called_when_it_succeeds(self):
        cloudflare = AsyncMock(return_value="Heyyy!")
        groq = AsyncMock(return_value="Groq reply")
        openai = AsyncMock(return_value="OpenAI reply")
        with (
            patch.object(ai_chat_provider, "_cloudflare_account_id", return_value="account"),
            patch.object(ai_chat_provider, "_cloudflare_api_token", return_value="token"),
            patch.object(ai_chat_provider, "_groq_api_key", return_value="groq-key"),
            patch.object(ai_chat_provider, "_openai_api_key", return_value="openai-key"),
            patch.object(ai_chat_provider, "_cloudflare_chat", cloudflare),
            patch.object(ai_chat_provider, "_groq_chat", groq),
            patch.object(ai_chat_provider, "_openai_chat", openai),
        ):
            result = await ai_chat_provider.ai_chat_completion([{"role": "user", "content": "Hi"}])
        self.assertEqual(result, "Heyyy!")
        cloudflare.assert_awaited_once()
        groq.assert_not_awaited()
        openai.assert_not_awaited()

    async def test_groq_is_used_when_cloudflare_fails(self):
        cloudflare = AsyncMock(side_effect=RuntimeError("Cloudflare unavailable"))
        groq = AsyncMock(return_value="Groq reply")
        openai = AsyncMock(return_value="OpenAI reply")
        with (
            patch.object(ai_chat_provider, "_cloudflare_account_id", return_value="account"),
            patch.object(ai_chat_provider, "_cloudflare_api_token", return_value="token"),
            patch.object(ai_chat_provider, "_groq_api_key", return_value="groq-key"),
            patch.object(ai_chat_provider, "_openai_api_key", return_value="openai-key"),
            patch.object(ai_chat_provider, "_cloudflare_chat", cloudflare),
            patch.object(ai_chat_provider, "_groq_chat", groq),
            patch.object(ai_chat_provider, "_openai_chat", openai),
        ):
            result = await ai_chat_provider.ai_chat_completion([{"role": "user", "content": "Hi"}])
        self.assertEqual(result, "Groq reply")
        cloudflare.assert_awaited_once()
        groq.assert_awaited_once()
        openai.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
