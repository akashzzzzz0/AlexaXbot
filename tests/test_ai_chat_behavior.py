import asyncio
import unittest
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

from aiogram.enums import ChatType

from bot.handlers import ai_chat


def _group_message(text: str, chat_id: int = -100123):
    return SimpleNamespace(
        text=text,
        message_id=(abs(hash((chat_id, text))) % 2000000000) + 1,
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
        ai_chat._smart_reply_message_count.clear()
        ai_chat._smart_reply_user_recent_messages.clear()
        ai_chat._smart_reply_spam_last_message.clear()
        ai_chat._smart_reply_spam_burst.clear()
        ai_chat._last_alexa_reply_at.clear()
        ai_chat._active_conversations.clear()
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

    def test_private_captioned_message_can_trigger_alexa(self):
        message = _group_message("ignored")
        message.chat.type = ChatType.PRIVATE
        message.text = None
        message.caption = "Alexa, is tasveer mein kya hai?"
        self.assertEqual(ai_chat._incoming_message_text(message), message.caption)
        self.assertTrue(asyncio.run(ai_chat.is_alexa_trigger(message)))

    def test_reply_context_is_included_only_for_alexa_replies(self):
        message = _group_message("Iska kya matlab?")
        message.reply_to_message = SimpleNamespace(
            from_user=SimpleNamespace(id=message.bot.id),
            text="Kal wali baat yaad hai?",
        )
        self.assertEqual(
            ai_chat._reply_context_text(message),
            "Kal wali baat yaad hai?",
        )
        message.reply_to_message.from_user.id = 222
        self.assertEqual(ai_chat._reply_context_text(message), "")

    def test_non_greeting_group_chat_does_not_trigger_unsolicited_reply(self):
        for index, text in enumerate(("kya haal hai?", "aaj group mein kya scene hai", "nice")):
            with self.subTest(text=text):
                # Fresh groups isolate the normal-message cadence from burst detection.
                self.assertFalse(asyncio.run(
                    ai_chat.is_alexa_trigger(_group_message(text, chat_id=-100900-index))
                ))

    def test_group_smart_reply_cooldowns_prevent_spam(self):
        message = _group_message("hello")
        self.assertTrue(asyncio.run(ai_chat.is_alexa_trigger(message)))
        # A different sender is subject to the group cooldown, not same-sender burst bypass.
        second = _group_message("kya scene hai?")
        second.from_user = SimpleNamespace(id=67890, is_bot=False)
        self.assertFalse(asyncio.run(ai_chat.is_alexa_trigger(second)))

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

    def test_religious_greetings_are_ignored_in_all_common_spellings(self):
        for index, text in enumerate((
            "Assalam o alaikum", "Assalamu alaikum", "As-salamu alaykum",
            "Salam", "Salaam", "AOA", "salam, kya haal hai?",
        )):
            with self.subTest(text=text):
                self.assertTrue(ai_chat._is_ignored_low_signal_message(text))
                self.assertFalse(asyncio.run(
                    ai_chat.is_alexa_trigger(_group_message(text, chat_id=-101000-index))
                ))

    def test_punctuation_only_messages_are_ignored(self):
        for index, text in enumerate((".", "..", "...", "?!", "—")):
            with self.subTest(text=text):
                self.assertTrue(ai_chat._is_ignored_low_signal_message(text))
                self.assertFalse(asyncio.run(
                    ai_chat.is_alexa_trigger(_group_message(text, chat_id=-101100-index))
                ))

    def test_real_text_is_not_treated_as_punctuation_noise(self):
        for text in ("hello", "kya haal hai?", "Alexa"):
            with self.subTest(text=text):
                self.assertFalse(ai_chat._is_ignored_low_signal_message(text))

    def test_provider_religious_greeting_response_is_suppressed(self):
        message = _group_message("Alexa hello")
        message.reply = AsyncMock()
        message.bot.send_chat_action = AsyncMock()
        key = (message.chat.id, message.from_user.id)
        ai_chat._history_loaded.add(key)
        with patch.object(ai_chat, "_ask_ai_with_typing", AsyncMock(return_value="Assalam o alaikum! Kya haal hain?")):
            asyncio.run(ai_chat._process_alexa_ai_chat(message))
        message.reply.assert_not_awaited()
        ai_chat._history_loaded.discard(key)


    def test_oy_oye_are_group_greeting_triggers(self):
        for index, text in enumerate(("oy", "oyyy", "oye", "oyeee")):
            with self.subTest(text=text):
                self.assertTrue(asyncio.run(
                    ai_chat.is_alexa_trigger(_group_message(text, chat_id=-100800-index))
                ))

    def test_goodnight_farewell_with_tomorrow_is_understood_as_a_goodbye(self):
        self.assertEqual(
            ai_chat._short_social_reply("Good night then we'll meet tomorrow okay", []),
            "Achha ji, good night! Kal milte hain. Apna khayal rakhna.",
        )

    def test_alexa_prefix_is_removed_before_short_intent_matching(self):
        self.assertEqual(
            ai_chat._short_social_reply("Good night then we'll meet tomorrow okay", []),
            "Achha ji, good night! Kal milte hain. Apna khayal rakhna.",
        )

    def test_india_time_and_date_questions_use_ist_clock(self):
        time_reply = ai_chat._current_ist_reply("Time kya ho raha hai?")
        date_reply = ai_chat._current_ist_reply("Aaj ki date kya hai?")
        self.assertIsNotNone(time_reply)
        self.assertIn("IST", time_reply)
        self.assertIsNotNone(date_reply)
        self.assertIn("India", date_reply)
        self.assertIsNone(ai_chat._current_ist_reply("I have time tomorrow"))

    def test_alexa_goodnight_does_not_call_ai_or_add_generic_clarification(self):
        message = _group_message("Alexa, Good night then we'll meet tomorrow okay")
        message.reply = AsyncMock()
        message.bot.send_chat_action = AsyncMock()
        key = (message.chat.id, message.from_user.id)
        ai_chat._conversations[key].clear()
        ai_chat._history_loaded.add(key)
        try:
            with (
                patch.object(ai_chat, "_ask_ai_with_typing", AsyncMock()) as provider,
                patch.object(ai_chat.asyncio, "to_thread", new=AsyncMock(return_value=None)),
            ):
                asyncio.run(ai_chat._process_alexa_ai_chat(message))
            provider.assert_not_awaited()
            message.reply.assert_awaited_once()
            reply_text = message.reply.await_args.args[0].casefold()
            self.assertIn("good night", reply_text)
            self.assertIn("kal milte hain", reply_text)
            self.assertNotIn("rephrase", reply_text)
            self.assertNotIn("doosre alfaaz", reply_text)
        finally:
            ai_chat._history_loaded.discard(key)
            ai_chat._conversations[key].clear()

    def test_good_morning_gets_a_natural_reply_and_repeats_are_contextual(self):
        first = ai_chat._short_social_reply("Good morning", [])
        self.assertIn(first, (
            "Good morning! Kese ho? ☀️",
            "Morning! Neend poori hui? 😄",
            "Good morning! Aaj kya haal hai? 🌼",
        ))
        repeated = ai_chat._short_social_reply(
            "Good morning",
            [("user", "Good morning"), ("model", "Good morning! Kese ho?")],
        )
        self.assertIn(repeated, (
            "Arrey, dobara good morning? 😂 Kya baat hai?",
            "Haan haan, good morning phir se! 😄",
            "Itni baar good morning? Aaj mood achha lag raha hai 😂",
        ))

    def test_short_followups_use_the_previous_turn(self):
        self.assertEqual(
            ai_chat._short_social_reply(
                "Kya samjhi",
                [("user", "Good morning"), ("model", "Good morning! Kese ho?")],
            ),
            "Yehi ke tum mujhe good morning keh rahe ho 😂",
        )
        self.assertIn(
            ai_chat._short_social_reply("Abe", []),
            ("Abe kya? Bolo, sun rahi hoon. 😂", "Haan ji, kya hua? 😄"),
        )

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
        self.assertIn("Roman Punjabi", system)
        self.assertEqual(provider.await_args.kwargs["max_tokens"], 350)
        self.assertEqual(provider.await_args.kwargs["temperature"], 0.58)
        self.assertIn("ALWAYS reply in Roman letters", system)
        self.assertIn("Avoid chatbot clichés", system)
        self.assertNotIn("Sirf casual Roman Hindi/Hinglish", system)
        self.assertIn("do not have a biological gender", system)
        self.assertIn("consistently feminine", system)
        self.assertIn("CRITICAL SELF-GENDER RULE", system)
        self.assertIn("NEVER use masculine self-forms", system)
        self.assertIn("de deta hoon", system)
        self.assertIn("answer honestly and briefly", system)
        self.assertIn("Never dodge a direct factual question", system)
        self.assertNotIn("do not confirm or deny", system)
        self.assertIn("GREETING BAN", system)
        self.assertIn("Never say, suggest, initiate", system)
        self.assertIn("UNDERSTANDING FIRST", system)
        self.assertIn("Be tolerant of typos", system)
        self.assertIn("Track negation", system)

    def test_bot_banter_has_at_least_fifteen_unique_lines(self):
        self.assertGreaterEqual(len(ai_chat._BOT_BANTER_REPLIES), 15)
        self.assertEqual(len(set(ai_chat._BOT_BANTER_REPLIES)), len(ai_chat._BOT_BANTER_REPLIES))

    def test_bot_banter_does_not_repeat_the_previous_reply(self):
        previous = ai_chat._BOT_BANTER_REPLIES[0]
        for _ in range(30):
            reply = ai_chat._bot_banter_reply([("user", "bot"), ("model", previous)])
            self.assertNotEqual(reply, previous)
            previous = reply

    def test_identity_questions_are_recognized_without_playful_evasion(self):
        for text in ("Are you an AI?", "Tum bot ho?", "Kya tum AI ho?", "Are you a robot?"):
            with self.subTest(text=text):
                self.assertTrue(ai_chat._is_identity_question(text))
        # A standalone "bot" is a casual nickname/tease, not a factual question.
        self.assertFalse(ai_chat._is_identity_question("bot"))


    def test_smart_group_prompt_does_not_force_hindi_for_other_languages(self):
        provider = AsyncMock(return_value="I'm good, thanks.")
        with patch.object(ai_chat, "ai_chat_completion", provider):
            asyncio.run(ai_chat._ask_ai("How are you?", [], smart_mode=True))
        system = provider.await_args.kwargs["system"]
        self.assertIn("Never output Urdu or Devanagari script", system)
        self.assertNotIn("Use natural Indian Roman Hindi/Hinglish", system)


    def test_direct_group_mention_gets_a_clarification_when_own_ai_has_no_answer(self):
        message = _group_message("Alexa kya haal hai?")
        message.reply = AsyncMock()
        message.bot.send_chat_action = AsyncMock()
        key = (message.chat.id, message.from_user.id)
        ai_chat._history_loaded.add(key)
        with patch.object(ai_chat, "_ask_ai_with_typing", AsyncMock(return_value="")):
            asyncio.run(ai_chat._process_alexa_ai_chat(message))
        message.reply.assert_awaited_once()
        self.assertIn("sun rahi hoon", message.reply.await_args.args[0].casefold())
        ai_chat._history_loaded.discard(key)

class AlexaChatHistoryWindowTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.previous_db = ai_chat._HISTORY_DB
        self.previous_initialized = ai_chat._history_initialized
        ai_chat._HISTORY_DB = Path(self.temp_dir.name) / "history.sqlite3"
        ai_chat._history_initialized = False

    def tearDown(self):
        ai_chat._HISTORY_DB = self.previous_db
        ai_chat._history_initialized = self.previous_initialized
        self.temp_dir.cleanup()

    def test_own_ai_receives_and_persists_twenty_recent_messages(self):
        self.assertEqual(ai_chat._HISTORY_LIMIT, 20)
        self.assertEqual(ai_chat._conversations[(987654, 123456)].maxlen, 20)
        for index in range(12):
            ai_chat._history_db_save(987654, 123456, f"user-{index}", f"assistant-{index}")
        history = ai_chat._history_db_load(987654, 123456)
        self.assertEqual(len(history), 20)
        self.assertEqual(history[0], ("user", "user-2"))
        self.assertEqual(history[-1], ("model", "assistant-11"))


if __name__ == "__main__":
    unittest.main()
