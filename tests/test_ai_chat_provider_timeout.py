import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from bot.services import ai_chat_provider


class AlexaChatProviderTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        ai_chat_provider._provider_retry_after.clear()
        ai_chat_provider._provider_rate_limited_until.clear()
        ai_chat_provider._provider_events.clear()

    async def test_grok_timeout_falls_through_to_openai(self):
        grok = AsyncMock(side_effect=asyncio.TimeoutError())
        openai = AsyncMock(return_value="OpenAI reply")
        with (
            patch.object(ai_chat_provider, "_groq_api_key", return_value=""),
            patch.object(ai_chat_provider, "_xai_api_key", return_value="grok-key"),
            patch.object(ai_chat_provider, "_openai_api_key", return_value="openai-key"),
            patch.object(ai_chat_provider, "_xai_chat", grok),
            patch.object(ai_chat_provider, "_openai_chat", openai),
        ):
            answer = await ai_chat_provider.ai_chat_completion(
                [{"role": "user", "content": "Hi"}]
            )
        self.assertEqual(answer, "OpenAI reply")
        grok.assert_awaited_once()
        openai.assert_awaited_once()

    async def test_grok_success_does_not_call_openai(self):
        grok = AsyncMock(return_value="Grok reply")
        openai = AsyncMock(return_value="OpenAI reply")
        with (
            patch.object(ai_chat_provider, "_xai_api_key", return_value="grok-key"),
            patch.object(ai_chat_provider, "_openai_api_key", return_value="openai-key"),
            patch.object(ai_chat_provider, "_xai_chat", grok),
            patch.object(ai_chat_provider, "_openai_chat", openai),
        ):
            answer = await ai_chat_provider.ai_chat_completion(
                [{"role": "user", "content": "Hi"}]
            )
        self.assertEqual(answer, "Grok reply")
        grok.assert_awaited_once()
        openai.assert_not_awaited()

    async def test_both_provider_failures_return_empty_without_fake_reply(self):
        grok = AsyncMock(side_effect=RuntimeError("Grok unavailable"))
        openai = AsyncMock(side_effect=RuntimeError("OpenAI unavailable"))
        with (
            patch.object(ai_chat_provider, "_xai_api_key", return_value="grok-key"),
            patch.object(ai_chat_provider, "_openai_api_key", return_value="openai-key"),
            patch.object(ai_chat_provider, "_xai_chat", grok),
            patch.object(ai_chat_provider, "_openai_chat", openai),
        ):
            answer = await ai_chat_provider.ai_chat_completion(
                [{"role": "user", "content": "Hi"}]
            )
        self.assertEqual(answer, "")
        grok.assert_awaited_once()
        openai.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
