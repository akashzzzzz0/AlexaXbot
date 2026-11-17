import unittest
from unittest.mock import AsyncMock, patch

from bot.services import ai_chat_provider


class AlexaOwnAIOnlyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        ai_chat_provider._provider_retry_after.clear()
        ai_chat_provider._provider_rate_limited_until.clear()
        ai_chat_provider._provider_events.clear()

    async def test_uses_only_alexa_own_ai(self):
        own = AsyncMock(return_value="Alexa Own AI reply")
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
            answer = await ai_chat_provider.ai_chat_completion(
                [{"role": "user", "content": "Hi"}],
                conversation_id="-100123:456",
            )
        self.assertEqual(answer, "Alexa Own AI reply")
        own.assert_awaited_once()
        self.assertEqual(own.await_args.kwargs["conversation_id"], "-100123:456")
        external.assert_not_awaited()

    async def test_does_not_call_any_external_provider_when_own_ai_is_missing(self):
        own = AsyncMock()
        external = AsyncMock()
        with (
            patch.object(ai_chat_provider, "_own_ai_url", return_value=""),
            patch.object(ai_chat_provider, "_own_ai_chat", own),
            patch.object(ai_chat_provider, "_gemini_chat", external),
            patch.object(ai_chat_provider, "_groq_chat", external),
            patch.object(ai_chat_provider, "_xai_chat", external),
            patch.object(ai_chat_provider, "_openai_chat", external),
            patch.object(ai_chat_provider, "_openrouter_chat", external),
        ):
            answer = await ai_chat_provider.ai_chat_completion(
                [{"role": "user", "content": "Hi"}]
            )
        self.assertEqual(answer, "")
        own.assert_not_awaited()
        external.assert_not_awaited()

    async def test_own_ai_error_does_not_trigger_external_fallback(self):
        own = AsyncMock(side_effect=RuntimeError("offline"))
        external = AsyncMock(return_value="External reply")
        with (
            patch.object(ai_chat_provider, "_own_ai_url", return_value="https://own-ai.example/v1/chat/completions"),
            patch.object(ai_chat_provider, "_own_ai_chat", own),
            patch.object(ai_chat_provider, "_gemini_chat", external),
            patch.object(ai_chat_provider, "_groq_chat", external),
        ):
            answer = await ai_chat_provider.ai_chat_completion(
                [{"role": "user", "content": "Hello"}]
            )
        self.assertEqual(answer, "")
        external.assert_not_awaited()

    async def test_no_provider_quota_events_are_emitted(self):
        self.assertEqual(ai_chat_provider.drain_provider_events(), [])


if __name__ == "__main__":
    unittest.main()
