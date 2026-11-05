import asyncio
from types import SimpleNamespace

from aiogram.enums import ChatType

from bot.handlers import ai_chat


def _group_message(text: str):
    return SimpleNamespace(
        text=text,
        from_user=SimpleNamespace(id=12345, is_bot=False),
        chat=SimpleNamespace(id=-100123, type=ChatType.SUPERGROUP),
        reply_to_message=None,
        entities=[],
        bot=SimpleNamespace(id=99999),
    )


def test_untagged_group_messages_from_any_member_trigger_chat():
    ai_chat._bot_username = ""
    for text in (
        "hi", "hello", "good morning", "good afternoon",
        "good evening", "good night", "kya haal hai?",
        "aaj group mein kya scene hai",
    ):
        assert asyncio.run(ai_chat.is_alexa_trigger(_group_message(text))) is True


def test_slash_commands_do_not_trigger_casual_chat():
    ai_chat._bot_username = ""
    assert asyncio.run(ai_chat.is_alexa_trigger(_group_message("/ban 123"))) is False


def test_all_day_greetings_are_recognized_for_replies_and_stickers():
    greetings = (
        "hi", "hello", "good morning", "good afternoon",
        "good evening", "good night", "morning", "afternoon",
        "evening", "gm", "gn",
    )
    for greeting in greetings:
        assert ai_chat._GREETING_RE.match(greeting)
    assert ai_chat._emotion_category("good afternoon") == "greeting"
    assert ai_chat._emotion_category("good evening") == "greeting"
