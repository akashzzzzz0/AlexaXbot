import unittest

import httpx
from unittest.mock import AsyncMock, patch

from bot.services import ai_chat_provider


class AlexaChatProviderRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        ai_chat_provider._provider_retry_after.clear()
        ai_chat_provider._provider_rate_limited_until.clear()
        ai_chat_provider._provider_events.clear()

    async def test_own_ai_is_primary_and_stops_chain_on_success(self):
        own_ai = AsyncMock(return_value="Own AI reply")
        gemini = AsyncMock(return_value="Gemini reply")
        groq = AsyncMock(return_value="Groq reply")
        with (
            patch.object(ai_chat_provider, "_own_ai_url", return_value="https://own-ai.example/v1/chat/completions"),
            patch.object(ai_chat_provider, "_own_ai_chat", own_ai),
            patch.object(ai_chat_provider, "_gemini_api_key", return_value="gemini-key"),
            patch.object(ai_chat_provider, "_gemini_chat", gemini),
            patch.object(ai_chat_provider, "_groq_api_keys", return_value=["groq-key"]),
            patch.object(ai_chat_provider, "_groq_chat", groq),
        ):
            result = await ai_chat_provider.ai_chat_completion([{"role": "user", "content": "Hi"}])
        self.assertEqual(result, "Own AI reply")
        own_ai.assert_awaited_once()
        gemini.assert_not_awaited()
        groq.assert_not_awaited()

    async def test_own_ai_no_answer_falls_through_without_cooldown(self):
        own_ai = AsyncMock(side_effect=ai_chat_provider.ProviderNoAnswer("no local intent"))
        gemini = AsyncMock(return_value="Gemini reply")
        with (
            patch.object(ai_chat_provider, "_own_ai_url", return_value="https://own-ai.example/v1/chat/completions"),
            patch.object(ai_chat_provider, "_own_ai_chat", own_ai),
            patch.object(ai_chat_provider, "_gemini_api_key", return_value="gemini-key"),
            patch.object(ai_chat_provider, "_gemini_chat", gemini),
            patch.object(ai_chat_provider, "_groq_api_keys", return_value=[]),
        ):
            result = await ai_chat_provider.ai_chat_completion([{"role": "user", "content": "Explain quantum physics"}])
        self.assertEqual(result, "Gemini reply")
        own_ai.assert_awaited_once()
        gemini.assert_awaited_once()
        self.assertNotIn("alexa_own_ai", ai_chat_provider._provider_retry_after)

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

    async def test_falls_through_own_ai_gemini_and_groq_until_one_answers(self):
        gemini = AsyncMock(side_effect=RuntimeError("Gemini unavailable"))
        groq = AsyncMock(side_effect=[RuntimeError("first Groq key unavailable"), "Second Groq key reply"])
        with (
            patch.object(ai_chat_provider, "_own_ai_url", return_value=""),
            patch.object(ai_chat_provider, "_gemini_api_key", return_value="gemini-key"),
            patch.object(ai_chat_provider, "_gemini_chat", gemini),
            patch.object(ai_chat_provider, "_groq_api_keys", return_value=["key-1", "key-2"]),
            patch.object(ai_chat_provider, "_groq_chat", groq),
        ):
            result = await ai_chat_provider.ai_chat_completion([{"role": "user", "content": "Hi"}])
        self.assertEqual(result, "Second Groq key reply")
        gemini.assert_awaited_once()
        self.assertEqual(groq.await_count, 2)

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



    async def test_checks_all_three_groq_keys_until_one_is_available(self):
        request = httpx.Request("POST", "https://api.groq.com/test")
        limited = httpx.Response(429, request=request, text='{"error":{"message":"rate limit"}}')
        groq = AsyncMock(side_effect=[
            httpx.HTTPStatusError("rate limited", request=request, response=limited),
            httpx.HTTPStatusError("rate limited", request=request, response=limited),
            "Third Groq key reply",
        ])
        with (
            patch.object(ai_chat_provider, "_gemini_api_key", return_value=""),
            patch.object(ai_chat_provider, "_groq_api_keys", return_value=["key-1", "key-2", "key-3"]),
            patch.object(ai_chat_provider, "_groq_chat", groq),
            patch.object(ai_chat_provider, "_xai_api_key", return_value="xai-key"),
            patch.object(ai_chat_provider, "_openai_api_key", return_value=""),
            patch.object(ai_chat_provider, "get_ollama_api_key", return_value=""),
        ):
            result = await ai_chat_provider.ai_chat_completion(
                [{"role": "user", "content": "Hi"}]
            )
        self.assertEqual(result, "Third Groq key reply")
        self.assertEqual(groq.await_count, 3)

    async def test_moves_through_all_three_groq_keys_when_gemini_and_groq_are_limited(self):
        request = httpx.Request("POST", "https://api.groq.com/test")
        limited = httpx.Response(429, request=request, text='{"error":{"message":"rate limit"}}')
        groq = AsyncMock(side_effect=[
            httpx.HTTPStatusError("rate limited", request=request, response=limited),
            httpx.HTTPStatusError("rate limited", request=request, response=limited),
            httpx.HTTPStatusError("rate limited", request=request, response=limited),
        ])
        gemini_request = httpx.Request("POST", "https://generativelanguage.googleapis.com/test")
        gemini = AsyncMock(side_effect=httpx.HTTPStatusError(
            "rate limited", request=gemini_request,
            response=httpx.Response(429, request=gemini_request, text='{"error":{"message":"quota"}}'),
        ))
        with (
            patch.object(ai_chat_provider, "_own_ai_url", return_value=""),
            patch.object(ai_chat_provider, "_gemini_api_key", return_value="gemini-key"),
            patch.object(ai_chat_provider, "_gemini_chat", gemini),
            patch.object(ai_chat_provider, "_groq_api_keys", return_value=["key-1", "key-2", "key-3"]),
            patch.object(ai_chat_provider, "_groq_chat", groq),
        ):
            result = await ai_chat_provider.ai_chat_completion([{"role": "user", "content": "Hi"}])
        self.assertEqual(result, "")
        gemini.assert_awaited_once()
        self.assertEqual(groq.await_count, 3)


    async def test_own_ai_is_tried_before_gemini_and_groq(self):
        own_ai = AsyncMock(return_value="Own AI reply")
        gemini = AsyncMock(return_value="Gemini reply")
        with (
            patch.object(ai_chat_provider, "_own_ai_url", return_value="https://own-ai.example/v1/chat/completions"),
            patch.object(ai_chat_provider, "_own_ai_chat", own_ai),
            patch.object(ai_chat_provider, "_gemini_api_key", return_value="gemini-key"),
            patch.object(ai_chat_provider, "_gemini_chat", gemini),
            patch.object(ai_chat_provider, "_groq_api_keys", return_value=["key-1"]),
        ):
            result = await ai_chat_provider.ai_chat_completion([{"role": "user", "content": "Hi"}])
        self.assertEqual(result, "Own AI reply")
        own_ai.assert_awaited_once()
        gemini.assert_not_awaited()

    async def test_own_ai_is_skipped_when_url_is_not_configured(self):
        own_ai = AsyncMock()
        with (
            patch.object(ai_chat_provider, "_gemini_api_key", return_value=""),
            patch.object(ai_chat_provider, "_groq_api_keys", return_value=[]),
            patch.object(ai_chat_provider, "_openrouter_api_key", return_value=""),
            patch.object(ai_chat_provider, "_xai_api_key", return_value=""),
            patch.object(ai_chat_provider, "_openai_api_key", return_value=""),
            patch.object(ai_chat_provider, "_own_ai_url", return_value=""),
            patch.object(ai_chat_provider, "_own_ai_chat", own_ai),
            patch.object(ai_chat_provider, "get_ollama_api_key", return_value=""),
        ):
            result = await ai_chat_provider.ai_chat_completion(
                [{"role": "user", "content": "Hi"}]
            )
        self.assertEqual(result, "")
        own_ai.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
