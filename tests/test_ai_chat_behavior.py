import asyncio
import unittest
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

from aiogram.enums import ChatType

from bot.handlers import ai_chat


def _group_message(text: str, chat_id: int = -100123):
    return SimpleNamespace(
        text=text,
        from_user=SimpleNamespace(id=12345, is_bot=False),
        chat=SimpleNamespace(id=chat_id, type=ChatType.SUPERGROUP),
        reply_to_message=None,
        entities=[],
        bot=SimpleNamespace(id=99999),
    )


class AlexaGroupChatBehaviorTests(unittest.TestCase):
    def setUp(self):
        ai_chat._bot_username = ""
        ai_chat._smart_reply_last_check.clear()
        ai_chat._smart_reply_user_last_check.clear()
        ai_chat._group_recent_activity.clear()

    def test_untagged_group_messages_from_any_member_trigger_chat(self):
        for index, text in enumerate((
            "hi", "hello", "good morning", "good afternoon",
            "good evening", "good night", "morning", "afternoon",
            "evening", "gm", "gn",
        )):
            with self.subTest(text=text):
                # Each sample represents a fresh group so the intentional
                # per-group anti-spam cooldown does not suppress later samples.
                self.assertTrue(asyncio.run(
                    ai_chat.is_alexa_trigger(_group_message(text, chat_id=-100123-index))
                ))

    def test_non_greeting_group_chat_does_not_trigger_unsolicited_reply(self):
        for text in ("kya haal hai?", "aaj group mein kya scene hai", "nice"):
            with self.subTest(text=text):
                self.assertFalse(asyncio.run(ai_chat.is_alexa_trigger(_group_message(text))))

    def test_group_smart_reply_cooldowns_prevent_spam(self):
        message = _group_message("hello")
        self.assertTrue(asyncio.run(ai_chat.is_alexa_trigger(message)))
        # A second non-greeting smart reply in the same group is held by cooldown.
        self.assertFalse(asyncio.run(ai_chat.is_alexa_trigger(_group_message("kya scene hai?"))))

    def test_smart_reply_ignores_active_member_to_member_conversation(self):
        chat_id = -100555
        ai_chat._group_recent_activity[chat_id].clear()
        ai_chat._group_recent_activity[chat_id].append((__import__("time").monotonic(), 111))
        ai_chat._group_recent_activity[chat_id].append((__import__("time").monotonic(), 222))
        self.assertFalse(asyncio.run(
            ai_chat.is_alexa_trigger(_group_message("kya haal hai?", chat_id=chat_id))
        ))

    def test_smart_reply_ignores_message_tagging_another_member(self):
        self.assertFalse(asyncio.run(
            ai_chat.is_alexa_trigger(_group_message("hello @someone123"))
        ))

    def test_smart_reply_ignores_short_member_mentions(self):
        self.assertFalse(asyncio.run(
            ai_chat.is_alexa_trigger(_group_message("kya haal hai @ali"))
        ))

    def test_smart_reply_ignores_hidden_text_mention_of_member(self):
        message = _group_message("kya haal hai")
        message.entities = [SimpleNamespace(
            type="text_mention",
            user=SimpleNamespace(id=222, is_bot=False),
        )]
        self.assertFalse(asyncio.run(ai_chat.is_alexa_trigger(message)))

    def test_reply_to_another_member_does_not_trigger_alexa(self):
        message = _group_message("haan bilkul")
        message.reply_to_message = SimpleNamespace(
            from_user=SimpleNamespace(id=222, is_bot=False)
        )
        self.assertFalse(asyncio.run(ai_chat.is_alexa_trigger(message)))

    def test_slash_commands_do_not_trigger_casual_chat(self):
        self.assertFalse(asyncio.run(ai_chat.is_alexa_trigger(_group_message("/ban 123"))))

    def test_oy_oye_are_group_greeting_triggers(self):
        for index, text in enumerate(("oy", "oyyy", "oye", "oyeee")):
            with self.subTest(text=text):
                self.assertTrue(asyncio.run(
                    ai_chat.is_alexa_trigger(_group_message(text, chat_id=-100800-index))
                ))

    def test_short_social_reply_handler_returns_only_short_varied_lines(self):
        examples = {
            "oy": ("Oyyyy 😂", "Kyaaa? 😂", "Haan bolo?"),
            "oye": ("Haan bolo?", "Oyeee 😂", "Ji, sun rahi hoon."),
            "helo": ("Oye hellooo 💗", "Hiii!", "Haan ji, bolo?"),
            "Alexa": ("Haan bolo?", "Ji, sun rahi hoon.", "Kya hua?"),
        }
        for text, choices in examples.items():
            with self.subTest(text=text):
                self.assertIn(ai_chat._short_social_reply(text), choices)

    def test_all_day_greetings_are_recognized_for_replies_and_stickers(self):
        greetings = (
            "oy", "oye", "hi", "hello", "good morning", "good afternoon",
            "good evening", "good night", "morning", "afternoon",
            "evening", "gm", "gn",
        )
        for greeting in greetings:
            with self.subTest(greeting=greeting):
                self.assertIsNotNone(ai_chat._GREETING_RE.match(greeting))
        self.assertEqual(ai_chat._emotion_category("good afternoon"), "greeting")
        self.assertEqual(ai_chat._emotion_category("good evening"), "greeting")


    def test_ai_prompt_restores_natural_multilingual_chat_style(self):
        provider = AsyncMock(return_value="Haan, samajh gayi.")
        with patch.object(ai_chat, "ai_chat_completion", provider):
            asyncio.run(ai_chat._ask_ai("kya haal hai?", [], smart_mode=False))
        system = provider.await_args.kwargs["system"]
        self.assertIn("Roman Urdu", system)
        self.assertIn("Bangla/Bengali", system)
        self.assertIn("Prefer the language and script the sender used", system)
        self.assertIn("Avoid chatbot clichés", system)
        self.assertNotIn("Sirf casual Roman Hindi/Hinglish", system)

    def test_smart_group_prompt_does_not_force_hindi_for_other_languages(self):
        provider = AsyncMock(return_value="I'm good, thanks.")
        with patch.object(ai_chat, "ai_chat_completion", provider):
            asyncio.run(ai_chat._ask_ai("How are you?", [], smart_mode=True))
        system = provider.await_args.kwargs["system"]
        self.assertIn("sender’s language and script naturally", system)
        self.assertNotIn("Use natural Indian Roman Hindi/Hinglish", system)


if __name__ == "__main__":
    unittest.main()
