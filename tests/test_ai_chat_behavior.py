import asyncio
import unittest
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

    def test_untagged_group_messages_from_any_member_trigger_chat(self):
        for index, text in enumerate((
            "hi", "hello", "good morning", "good afternoon",
            "good evening", "good night", "kya haal hai?",
            "aaj group mein kya scene hai",
        )):
            with self.subTest(text=text):
                # Each sample represents a fresh group so the intentional
                # per-group anti-spam cooldown does not suppress later samples.
                self.assertTrue(asyncio.run(
                    ai_chat.is_alexa_trigger(_group_message(text, chat_id=-100123-index))
                ))

    def test_group_smart_reply_cooldowns_prevent_spam(self):
        message = _group_message("hi")
        self.assertTrue(asyncio.run(ai_chat.is_alexa_trigger(message)))
        # A second greeting in the same group is held by the 15-second group cooldown.
        self.assertFalse(asyncio.run(ai_chat.is_alexa_trigger(_group_message("hello"))))

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

    def test_reply_to_another_member_never_triggers_alexa(self):
        message = _group_message("kya haal hai?")
        message.reply_to_message = SimpleNamespace(
            from_user=SimpleNamespace(id=222, is_bot=False),
            text="hello",
        )
        self.assertFalse(asyncio.run(ai_chat.is_alexa_trigger(message)))

    def test_reply_to_alexa_still_triggers_alexa(self):
        message = _group_message("continue")
        message.reply_to_message = SimpleNamespace(
            from_user=SimpleNamespace(id=99999, is_bot=True),
            text="previous Alexa reply",
        )
        self.assertTrue(asyncio.run(ai_chat.is_alexa_trigger(message)))

    def test_slash_commands_do_not_trigger_casual_chat(self):
        self.assertFalse(asyncio.run(ai_chat.is_alexa_trigger(_group_message("/ban 123"))))

    def test_all_day_greetings_are_recognized_for_replies_and_stickers(self):
        greetings = (
            "hi", "hello", "good morning", "good afternoon",
            "good evening", "good night", "morning", "afternoon",
            "evening", "gm", "gn",
        )
        for greeting in greetings:
            with self.subTest(greeting=greeting):
                self.assertIsNotNone(ai_chat._GREETING_RE.match(greeting))
        self.assertEqual(ai_chat._emotion_category("good afternoon"), "greeting")
        self.assertEqual(ai_chat._emotion_category("good evening"), "greeting")


if __name__ == "__main__":
    unittest.main()
