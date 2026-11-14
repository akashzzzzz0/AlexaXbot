import unittest

from bot.handlers.ai_chat import _local_chat_fallback


class AlexaLocalChatFallbackTests(unittest.TestCase):
    def test_respects_kuch_nahi_without_asking_again(self):
        answer = _local_chat_fallback("Kuch nahi", [("user", "Kya hua?"), ("model", "Kya hua?")])
        self.assertIn("rehne dete hain", answer.casefold())
        self.assertNotIn("kya hua", answer.casefold())

    def test_respects_user_repeating_nothing_happened(self):
        answer = _local_chat_fallback(
            "Kuch nahi hua bola na",
            [("user", "Kuch nahi"), ("model", "Achha, theek hai. Phir rehne dete hain.")],
        )
        self.assertIn("ab nahi poochungi", answer.casefold())

    def test_kya_bataun_does_not_repeat_prompt(self):
        answer = _local_chat_fallback(
            "Kya btaun",
            [("user", "Kuch nahi"), ("model", "Phir kya hua?")],
        )
        self.assertNotIn("bolo na", answer.casefold())
        self.assertNotIn("aage batao", answer.casefold())
        self.assertTrue("topic" in answer.casefold() or "mann" in answer.casefold())

    def test_respects_explicit_request_to_drop_topic(self):
        answer = _local_chat_fallback("Chhoro, mat pooch", [])
        self.assertIn("nahi poochungi", answer.casefold())


if __name__ == "__main__":
    unittest.main()
