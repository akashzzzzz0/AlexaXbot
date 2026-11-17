import unittest
from unittest.mock import AsyncMock, patch

from bot.services import ai_chat_provider


class AlexaOwnAITimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_own_ai_timeout_does_not_fall_through_to_external_services(self):
        own = AsyncMock(side_effect=TimeoutError())
        external = AsyncMock(return_value="External reply")
        with (
            patch.object(ai_chat_provider, "_own_ai_url", return_value="https://own-ai.example/v1/chat/completions"),
            patch.object(ai_chat_provider, "_own_ai_chat", own),
            patch.object(ai_chat_provider, "_gemini_chat", external),
            patch.object(ai_chat_provider, "_groq_chat", external),
            patch.object(ai_chat_provider, "_xai_chat", external),
            patch.object(ai_chat_provider, "_openai_chat", external),
            patch.object(ai_chat_provider, "_openrouter_chat", external),
        ):
            answer = await ai_chat_provider.ai_chat_completion([{"role": "user", "content": "Hi"}])
        self.assertEqual(answer, "")
        own.assert_awaited_once()
        external.assert_not_awaited()

    async def test_own_ai_success_returns_reply(self):
        own = AsyncMock(return_value="Own AI reply")
        with (
            patch.object(ai_chat_provider, "_own_ai_url", return_value="https://own-ai.example/v1/chat/completions"),
            patch.object(ai_chat_provider, "_own_ai_chat", own),
        ):
            answer = await ai_chat_provider.ai_chat_completion([{"role": "user", "content": "Hi"}])
        self.assertEqual(answer, "Own AI reply")


if __name__ == "__main__":
    unittest.main()
