import unittest
from unittest.mock import AsyncMock, patch

from bot.services import ai_chat_provider


class AlexaChatProviderRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        ai_chat_provider._provider_retry_after.clear()
        ai_chat_provider._provider_rate_limited_until.clear()
        ai_chat_provider._provider_events.clear()

    async def test_gemini_is_primary_and_stops_chain_on_success(self):
        gemini = AsyncMock(return_value="Gemini reply")
        groq = AsyncMock(return_value="Groq reply")
        with (
            patch.object(ai_chat_provider, "_gemini_api_key", return_value="gemini-key"),
            patch.object(ai_chat_provider, "_gemini_chat", gemini),
            patch.object(ai_chat_provider, "_groq_api_keys", return_value=["groq-key"]),
            patch.object(ai_chat_provider, "_groq_chat", groq),
            patch.object(ai_chat_provider, "_xai_api_key", return_value="xai-key"),
            patch.object(ai_chat_provider, "_openai_api_key", return_value="openai-key"),
            patch.object(ai_chat_provider, "get_ollama_api_key", return_value="ollama-key"),
        ):
            result = await ai_chat_provider.ai_chat_completion([{"role": "user", "content": "Hi"}])
        self.assertEqual(result, "Gemini reply")
        gemini.assert_awaited_once()
        groq.assert_not_awaited()

    async def test_gemini_gets_enough_time_to_finish_its_http_request(self):
        gemini = AsyncMock(return_value="Gemini reply")
        observed_timeouts = []

        async def run_with_recorded_timeout(awaitable, timeout):
            observed_timeouts.append(timeout)
            return await awaitable

        with (
            patch.object(ai_chat_provider, "_gemini_api_key", return_value="gemini-key"),
            patch.object(ai_chat_provider, "_gemini_chat", gemini),
            patch.object(ai_chat_provider, "_groq_api_keys", return_value=[]),
            patch.object(ai_chat_provider, "_xai_api_key", return_value=""),
            patch.object(ai_chat_provider, "_openai_api_key", return_value=""),
            patch.object(ai_chat_provider, "get_ollama_api_key", return_value=""),
            patch.object(ai_chat_provider.asyncio, "wait_for", side_effect=run_with_recorded_timeout),
        ):
            result = await ai_chat_provider.ai_chat_completion(
                [{"role": "user", "content": "Hi"}]
            )

        self.assertEqual(result, "Gemini reply")
        self.assertEqual(observed_timeouts, [12.0])

    async def test_falls_through_all_providers_until_one_answers(self):
        gemini = AsyncMock(side_effect=RuntimeError("Gemini unavailable"))
        groq = AsyncMock(side_effect=RuntimeError("Groq unavailable"))
        grok = AsyncMock(side_effect=RuntimeError("Grok unavailable"))
        openai = AsyncMock(side_effect=RuntimeError("OpenAI unavailable"))
        ollama = AsyncMock(return_value="Ollama reply")
        with (
            patch.object(ai_chat_provider, "_gemini_api_key", return_value="gemini-key"),
            patch.object(ai_chat_provider, "_gemini_chat", gemini),
            patch.object(ai_chat_provider, "_groq_api_keys", return_value=["groq-key"]),
            patch.object(ai_chat_provider, "_groq_chat", groq),
            patch.object(ai_chat_provider, "_xai_api_key", return_value="xai-key"),
            patch.object(ai_chat_provider, "_xai_chat", grok),
            patch.object(ai_chat_provider, "_openai_api_key", return_value="openai-key"),
            patch.object(ai_chat_provider, "_openai_chat", openai),
            patch.object(ai_chat_provider, "get_ollama_api_key", return_value="ollama-key"),
            patch.object(ai_chat_provider, "ollama_chat", ollama),
        ):
            result = await ai_chat_provider.ai_chat_completion([{"role": "user", "content": "Hi"}])
        self.assertEqual(result, "Ollama reply")
        for provider in (gemini, groq, grok, openai, ollama):
            provider.assert_awaited_once()

    async def test_rotates_groq_keys_before_moving_to_next_provider(self):
        groq = AsyncMock(side_effect=[RuntimeError("first key failed"), "Second key reply"])
        with (
            patch.object(ai_chat_provider, "_gemini_api_key", return_value=""),
            patch.object(ai_chat_provider, "_groq_api_keys", return_value=["key-1", "key-2"]),
            patch.object(ai_chat_provider, "_groq_chat", groq),
            patch.object(ai_chat_provider, "_xai_api_key", return_value="xai-key"),
            patch.object(ai_chat_provider, "_openai_api_key", return_value=""),
            patch.object(ai_chat_provider, "get_ollama_api_key", return_value=""),
        ):
            result = await ai_chat_provider.ai_chat_completion([{"role": "user", "content": "Hi"}])
        self.assertEqual(result, "Second key reply")
        self.assertEqual(groq.await_count, 2)

    async def test_skips_unconfigured_providers(self):
        gemini = AsyncMock()
        groq = AsyncMock()
        grok = AsyncMock()
        openai = AsyncMock()
        ollama = AsyncMock()
        with (
            patch.object(ai_chat_provider, "_gemini_api_key", return_value=""),
            patch.object(ai_chat_provider, "_gemini_chat", gemini),
            patch.object(ai_chat_provider, "_groq_api_keys", return_value=[]),
            patch.object(ai_chat_provider, "_groq_chat", groq),
            patch.object(ai_chat_provider, "_xai_api_key", return_value=""),
            patch.object(ai_chat_provider, "_xai_chat", grok),
            patch.object(ai_chat_provider, "_openai_api_key", return_value=""),
            patch.object(ai_chat_provider, "_openai_chat", openai),
            patch.object(ai_chat_provider, "get_ollama_api_key", return_value=""),
            patch.object(ai_chat_provider, "ollama_chat", ollama),
        ):
            result = await ai_chat_provider.ai_chat_completion([{"role": "user", "content": "Hi"}])
        self.assertEqual(result, "")
        for provider in (gemini, groq, grok, openai, ollama):
            provider.assert_not_awaited()


    async def test_groq_cooldown_skips_do_not_reemit_owner_429_event(self):
        retry_at = ai_chat_provider.time.monotonic() + 300
        ai_chat_provider._provider_retry_after["groq_key_1"] = retry_at
        ai_chat_provider._provider_rate_limited_until["groq_key_1"] = retry_at
        with (
            patch.object(ai_chat_provider, "_gemini_api_key", return_value=""),
            patch.object(ai_chat_provider, "_groq_api_keys", return_value=["groq-key"]),
            patch.object(ai_chat_provider, "_xai_api_key", return_value=""),
            patch.object(ai_chat_provider, "_openai_api_key", return_value=""),
            patch.object(ai_chat_provider, "get_ollama_api_key", return_value=""),
        ):
            result = await ai_chat_provider.ai_chat_completion(
                [{"role": "user", "content": "Hi"}]
            )
        self.assertEqual(result, "")
        self.assertEqual(ai_chat_provider.drain_provider_events(), [])


if __name__ == "__main__":
    unittest.main()
