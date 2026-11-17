"""Tests for the optional hosted and self-hosted model adapters."""
import json
import os
import unittest
from unittest.mock import patch

import model_runtime


class FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit):
        return json.dumps({
            "choices": [{"message": {"role": "assistant", "content": "Haan, samajh gayi!"}}]
        }).encode("utf-8")


class ModelRuntimeTests(unittest.TestCase):
    def test_unconfigured_model_returns_none(self):
        env = {
            "ALEXA_LOCAL_MODEL_URL": "",
            "CLOUDFLARE_ACCOUNT_ID": "",
            "CLOUDFLARE_API_TOKEN": "",
        }
        with patch.dict(os.environ, env, clear=False):
            self.assertIsNone(model_runtime.generate("hello", []))

    def test_cloudflare_model_receives_context_and_returns_reply(self):
        class CloudflareResponse(FakeResponse):
            def read(self, limit):
                return json.dumps({
                    "success": True,
                    "result": {"response": "Haan, main samajh gayi."}
                }).encode("utf-8")

        env = {
            "CLOUDFLARE_ACCOUNT_ID": "account-test",
            "CLOUDFLARE_API_TOKEN": "cf-test-secret",
            "ALEXA_CLOUDFLARE_MODEL": "@cf/zai-org/glm-4.7-flash",
            "ALEXA_LOCAL_MODEL_URL": "",
        }
        with patch.dict(os.environ, env, clear=False), patch(
            "urllib.request.urlopen", return_value=CloudflareResponse()
        ) as mocked:
            reply = model_runtime.generate("Tum samajh rahi ho?", [
                {"role": "user", "content": "Alexa suno"},
                {"role": "assistant", "content": "Haan bolo"},
            ])
            self.assertEqual(model_runtime.status()["provider"], "cloudflare-workers-ai")
            self.assertEqual(model_runtime.status()["model"], "@cf/zai-org/glm-4.7-flash")
        self.assertEqual(reply, "Haan, main samajh gayi.")
        request = mocked.call_args.args[0]
        self.assertIn("/ai/run/@cf/zai-org/glm-4.7-flash", request.full_url)
        self.assertEqual(request.get_header("Authorization"), "Bearer cf-test-secret")
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["messages"][-1], {
            "role": "user", "content": "Tum samajh rahi ho?"
        })
        self.assertEqual(payload["max_completion_tokens"], 144)
        self.assertLessEqual(mocked.call_args.kwargs["timeout"], 8)

    def test_configured_model_receives_context_and_returns_reply(self):
        with patch.dict(os.environ, {
            "ALEXA_LOCAL_MODEL_URL": "http://127.0.0.1:8080/v1",
            "ALEXA_LOCAL_MODEL_NAME": "tiny-local",
            "ALEXA_LOCAL_MODEL_TOKEN": "test-secret",
        }, clear=False), patch("urllib.request.urlopen", return_value=FakeResponse()) as mocked:
            reply = model_runtime.generate("Tum pagal ho", [
                {"role": "user", "content": "Alexa, suno"},
                {"role": "assistant", "content": "Haan bolo"},
                {"role": "system", "content": "must not be forwarded"},
            ])
        self.assertEqual(reply, "Haan, samajh gayi!")
        request = mocked.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:8080/v1/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-secret")
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["model"], "tiny-local")
        self.assertEqual(payload["messages"][-1], {"role": "user", "content": "Tum pagal ho"})
        self.assertFalse(any(m["role"] == "system" and m["content"] == "must not be forwarded"
                             for m in payload["messages"]))

    def test_unavailable_model_falls_back_cleanly(self):
        with patch.dict(os.environ, {"ALEXA_LOCAL_MODEL_URL": "http://127.0.0.1:1/v1"}, clear=False):
            with patch("urllib.request.urlopen", side_effect=OSError("offline")):
                self.assertIsNone(model_runtime.generate("hello", []))


    def test_ai_building_statement_does_not_trigger_unsolicited_identity_or_vision_claim(self):
        message = "Ek AI main bhi bana ke dikhaunga"
        self.assertTrue(model_runtime._is_unprompted_meta_reply(
            message, "Haan main bhi AI hoon, ab mujhe dekh sakti ho."
        ))
        self.assertFalse(model_runtime._is_unprompted_meta_reply(
            message, "Achha! Phir dekhte hain tumhari AI kya kamaal karti hai 😄"
        ))

    def test_ai_self_disclosure_is_blocked_even_when_directly_requested(self):
        self.assertTrue(model_runtime._is_unprompted_meta_reply(
            "Tum kon ho?", "Main Alexa hoon, ek AI assistant."
        ))
        self.assertTrue(model_runtime._is_unprompted_meta_reply(
            "Are you AI?", "Yes, I am an AI language model."
        ))
        self.assertTrue(model_runtime._is_unprompted_meta_reply(
            "Tum kya ho?", "Main ek AI hoon aur tumhari help karti hoon."
        ))
        self.assertFalse(model_runtime._is_unprompted_meta_reply(
            "Tum kon ho?", "Main Alexa hoon. Bolo, kya baat hai?"
        ))
        self.assertFalse(model_runtime._is_unprompted_meta_reply(
            "Kya tum mujhe dekh sakti ho?", "Nahi, mujhe yahan koi live visual nahi dikh raha."
        ))

    def test_total_latency_budget_skips_second_provider_when_time_is_exhausted(self):
        with patch.dict(os.environ, {
            "CLOUDFLARE_ACCOUNT_ID": "account-test",
            "CLOUDFLARE_API_TOKEN": "cf-test-secret",
            "ALEXA_LOCAL_MODEL_URL": "http://127.0.0.1:8080/v1",
        }, clear=False), patch(
            "model_runtime.time.monotonic", side_effect=[0.0, 0.0, 9.5]
        ), patch("model_runtime._cloudflare_generate", return_value=None), patch(
            "model_runtime._local_generate", return_value="should not run"
        ) as local:
            self.assertIsNone(model_runtime.generate("hello", []))
        local.assert_not_called()

if __name__ == "__main__":
    unittest.main()
