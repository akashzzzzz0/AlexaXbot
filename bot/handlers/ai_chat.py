"""Mention/reply/greeting-based Ollama-backed chat for ALEXA groups.

ALEXA stays silent on missing configuration, API errors, or network failures.
"""
import asyncio
import html
import logging
import re
import sqlite3
import threading
import time
import os
import random
from collections import defaultdict, deque
from pathlib import Path

import httpx
from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.types import Message, ReactionTypeEmoji

from bot.config import settings
from bot.services.ollama_ai import ollama_chat

router = Router(name="alexa_ai_chat")
logger = logging.getLogger(__name__)
_missing_key_logged = False
_conversations = defaultdict(lambda: deque(maxlen=12))
_HISTORY_DB = Path("alexa_chat_history.db")
_HISTORY_LIMIT = 30
_history_loaded = set()
def _reaction_for_text(text: str) -> str | None:
    """Return an occasional context-based reaction, not one on every message."""
    value = text.casefold()
    options = [
        (r"\b(haha+|hehe+|lol|lmao|rofl)\b|😂|🤣|😹", "😂"),
        (r"\b(thanks?|thank you|shukriya|meherbani|love you|i love)\b|❤️|🥰", "❤️"),
        (r"\b(wow|seriously|really|no way|omg|what)\b|😮|😱|🤯", "😮"),
        (r"\b(sad|upset|cry|miss you|bura lag|mood off|dukhi)\b|😢|😭|🥺", "🥺"),
    ]
    for pattern, emoji in options:
        if re.search(pattern, value, re.IGNORECASE):
            return emoji if random.random() < 0.42 else None
    return None


async def _maybe_react(message: Message) -> None:
    if not message.from_user or message.from_user.is_bot:
        return
    emoji = _reaction_for_text(message.text or message.caption or "")
    if not emoji:
        return
    try:
        await message.bot.set_message_reaction(
            chat_id=message.chat.id,
            message_id=message.message_id,
            reaction=[ReactionTypeEmoji(emoji=emoji)],
        )
    except Exception:
        # Reactions are optional; unsupported chat permissions must not affect chat.
        pass


# Emotion-aware sticker selection from the public pack configured below.
_STICKER_SET_NAME = os.getenv("ALEXA_STICKER_SET_NAME", "video6104468803_by_vzrobot").strip()
_STICKER_SET_CACHE: list[tuple[str, str]] | None = None


def _emotion_category(text: str) -> str | None:
    value = text.casefold()
    rules = (
        ("funny", r"haha|hehe|lol|lmao|mazak|mazaak|😂|🤣|😹"),
        ("sad", r"sad|cry|dukhi|udaas|udas|mood off|😢|😭|🥺|💔"),
        ("angry", r"angry|gussa|ghussa|naraz|😡|🤬|😠|😤"),
        ("love", r"thanks|thank you|shukriya|pyaar|pyar|cute|❤️|😍|🥰|😘|💕"),
        ("surprise", r"wow|omg|shocked|surprised|😮|😱|🤯|😳"),
        ("greeting", r"^\s*(hi+|hello+|hey+|salam|aoa|bye+|good night|good morning)\b"),
        ("attitude", r"attitude|boss|savage|swag|😎|😏|🔥"),
    )
    for category, pattern in rules:
        if re.search(pattern, value, re.IGNORECASE):
            return category
    return None


_EMOTION_EMOJIS = {
    "funny": {"😂", "🤣", "😹", "😆", "😁", "😜", "🤪"},
    "sad": {"😢", "😭", "🥺", "😔", "💔", "😞"},
    "angry": {"😡", "🤬", "😠", "😤"},
    "love": {"❤️", "❤", "😍", "🥰", "😘", "💕", "💖", "💗", "💞"},
    "surprise": {"😮", "😱", "🤯", "😳", "🙀"},
    "greeting": {"👋", "😊", "🤗", "🙋", "😄"},
    "attitude": {"😎", "😏", "🔥"},
}


async def _maybe_send_sticker(message: Message, user_text: str) -> None:
    """Send an occasional sticker matching the user's emotional cue."""
    global _STICKER_SET_CACHE
    category = _emotion_category(user_text)
    if not category or random.random() >= 0.35:
        return
    if _STICKER_SET_CACHE is None:
        try:
            sticker_set = await message.bot.get_sticker_set(name=_STICKER_SET_NAME)
            _STICKER_SET_CACHE = [
                (sticker.file_id, getattr(sticker, "emoji", "") or "")
                for sticker in sticker_set.stickers
                if getattr(sticker, "file_id", None)
            ]
        except Exception:
            _STICKER_SET_CACHE = []
    matching = [
        file_id for file_id, emoji in (_STICKER_SET_CACHE or [])
        if emoji in _EMOTION_EMOJIS.get(category, set())
    ]
    if matching:
        try:
            await message.answer_sticker(random.choice(matching))
        except Exception:
            pass
        return
    # If pack lookup failed, keep existing manually configured IDs as fallback.
    if _STICKER_SET_CACHE:
        return
    raw_ids = os.getenv("ALEXA_STICKER_FILE_IDS", "").strip()
    if not raw_ids:
        try:
            from bot.gemini_secret import ALEXA_STICKER_FILE_IDS
            raw_ids = str(ALEXA_STICKER_FILE_IDS or "").strip()
        except (ImportError, AttributeError, TypeError):
            raw_ids = ""
    file_ids = [item.strip() for item in raw_ids.split(",") if item.strip()]
    if file_ids:
        try:
            await message.answer_sticker(random.choice(file_ids))
        except Exception:
            pass


# Schema setup is cached per process; avoid running CREATE TABLE/INDEX on every reply.
_history_initialized = False
_history_init_lock = threading.Lock()


def _history_db_init() -> None:
    """Create the persistent history schema once per process."""
    global _history_initialized
    if _history_initialized:
        return
    with _history_init_lock:
        if _history_initialized:
            return
        with sqlite3.connect(_HISTORY_DB, timeout=10) as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS alexa_chat_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('user', 'model')),
                    message TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_alexa_history_chat_user_id "
                "ON alexa_chat_history(chat_id, user_id, id)"
            )
        _history_initialized = True


def _history_db_load(chat_id: int, user_id: int) -> list[tuple[str, str]]:
    _history_db_init()
    with sqlite3.connect(_HISTORY_DB, timeout=10) as db:
        rows = db.execute(
            "SELECT role, message FROM alexa_chat_history "
            "WHERE chat_id = ? AND user_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, user_id, _HISTORY_LIMIT),
        ).fetchall()
    return [(role, message) for role, message in reversed(rows)]


def _history_db_save(chat_id: int, user_id: int, user_text: str, answer: str) -> None:
    _history_db_init()
    with sqlite3.connect(_HISTORY_DB, timeout=10) as db:
        db.executemany(
            "INSERT INTO alexa_chat_history (chat_id, user_id, role, message) "
            "VALUES (?, ?, ?, ?)",
            [
                (chat_id, user_id, "user", user_text[:4000]),
                (chat_id, user_id, "model", answer[:4000]),
            ],
        )
        # Keep recent context only; old chats still inform the latest saved window.
        db.execute(
            "DELETE FROM alexa_chat_history WHERE chat_id = ? AND user_id = ? "
            "AND id NOT IN (SELECT id FROM alexa_chat_history "
            "WHERE chat_id = ? AND user_id = ? ORDER BY id DESC LIMIT 300)",
            (chat_id, user_id, chat_id, user_id),
        )

# A user who addresses Alexa keeps a short, per-chat conversation window.
# This lets follow-up messages work without repeatedly tagging/replying to her.
_active_until = {}
_SESSION_SECONDS = 10 * 60
_bot_username = None

# Opportunistic group replies are deliberately rate-limited to avoid flooding chats.
_smart_reply_last_check: dict[int, float] = {}
_smart_reply_user_last_check: dict[tuple[int, int], float] = {}
_SMART_REPLY_GROUP_COOLDOWN_SECONDS = 15.0
_SMART_REPLY_USER_COOLDOWN_SECONDS = 45.0

_GREETING_RE = re.compile(
    r"^\s*(?:hi+|hello+|helo+|hey+|salam|assalamualaikum|aoa)(?:\b.*)?$",
    re.IGNORECASE,
)
_ALEXA_PREFIX_RE = re.compile(r"^\s*alexa\b[\s,!:.-]*(.*)$", re.IGNORECASE)


def _looks_like_smart_reply_candidate(text: str) -> bool:
    """Filter low-value group chatter before spending an Ollama request."""
    value = text.strip().casefold()
    if len(value) < 8:
        return False
    if re.fullmatch(r"(?:ok(?:ay)?|acha|hmm+|huh|nice|same|true|right|👍+|👌+|❤️+|😂+|🤣+|🙏+)[.!?\s]*", value):
        return False
    if re.search(r"[?؟]|\b(?:kya|kyun|kaise|kaisa|kaisi|kab|kahan|kon|kaun|who|what|why|how|when|where|which|anyone|suggest|opinion|batao|help)\b|😂|🤣|😹|😢|😭|😮|😱", value, re.IGNORECASE):
        return True
    # Longer conversational statements may be worth a response; the model decides.
    return len(value.split()) >= 7 or len(value) >= 45


def _should_smart_reply(message: Message, text: str) -> bool:
    """Allow one model-judged, unaddressed reply per group/user cooldown."""
    if not _looks_like_smart_reply_candidate(text):
        return False
    now = time.monotonic()
    chat_id = message.chat.id
    user_id = message.from_user.id if message.from_user else 0
    user_key = (chat_id, user_id)
    if now - _smart_reply_last_check.get(chat_id, 0.0) < _SMART_REPLY_GROUP_COOLDOWN_SECONDS:
        return False
    if now - _smart_reply_user_last_check.get(user_key, 0.0) < _SMART_REPLY_USER_COOLDOWN_SECONDS:
        return False
    _smart_reply_last_check[chat_id] = now
    _smart_reply_user_last_check[user_key] = now
    return True


async def is_alexa_trigger(message: Message) -> bool:
    """Trigger direct chat normally and selectively sample group messages for smart replies."""
    global _bot_username
    text = (message.text or "").strip()
    if not text or not message.from_user or message.from_user.is_bot:
        return False

    # Keep slash commands available to their existing feature handlers in every chat.
    if text.startswith("/"):
        return False

    # In private chat, every ordinary text message starts/continues Alexa chat.
    if message.chat.type == ChatType.PRIVATE:
        return True

    replied = message.reply_to_message
    # If the user is replying to anyone except Alexa, never join that thread,
    # even when this user recently chatted with Alexa or the text says "hi".
    if replied:
        if not replied.from_user or replied.from_user.id != message.bot.id:
            return False
        return True

    if _bot_username is None:
        try:
            me = await message.bot.get_me()
            _bot_username = (me.username or "").casefold()
        except Exception:
            _bot_username = ""

    if _bot_username and re.search(
        rf"(?<!\w)@{re.escape(_bot_username)}\b", text, re.IGNORECASE
    ):
        return True

    # Do not answer messages directed at another member. Check this before
    # greeting/session triggers (e.g. "hi @someone") so Alexa doesn't interrupt.
    mentioned_usernames = re.findall(r"(?<!\w)@([A-Za-z0-9_]{5,})", text)
    if any(name.casefold() != _bot_username for name in mentioned_usernames):
        return False
    for entity in (message.entities or []):
        if (
            getattr(entity.type, "value", entity.type) == "text_mention"
            and entity.user
            and entity.user.id != message.bot.id
        ):
            return False

    # Recognize Alexa's name anywhere in a group message, not only at the start.
    # This makes natural phrases like "yaar Alexa zara help" trigger reliably.
    if re.search(r"(?i)(?<!\w)alexa(?!\w)", text):
        return True

    if _ALEXA_PREFIX_RE.match(text):
        return True

    # In groups, don't respond to generic greetings on their own. Require
    # an explicit Alexa address, a reply to Alexa, or a short follow-up in an
    # already active one-to-one conversation with Alexa.
    session_key = (message.chat.id, message.from_user.id)
    if time.monotonic() < _active_until.get(session_key, 0.0):
        return True
    return _should_smart_reply(message, text)


async def _ask_ollama_with_typing(
    message: Message, user_text: str, history: deque, *, smart_mode: bool = False
) -> str:
    """Keep Telegram's typing indicator active while Alexa prepares an AI reply."""
    # Keep the typing indicator alive while the model is working.
    async def _typing_hint() -> None:
        try:
            while True:
                await message.bot.send_chat_action(
                    chat_id=message.chat.id,
                    action="typing",
                )
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Typing is optional; do not affect response generation.
            pass

    typing_task = asyncio.create_task(_typing_hint())
    try:
        return await _ask_ollama(user_text, history, smart_mode=smart_mode)
    finally:
        typing_task.cancel()


async def _ask_ollama(
    user_text: str, history: deque, *, smart_mode: bool = False
) -> str:
    """Generate a casual reply through the shared Ollama provider."""
    system_instruction = (
        "You are Alexa, a lively feminine Telegram chat persona fluent in Pakistani Roman Urdu/Hinglish. "
        "Sound spontaneous, warm, playful, socially aware, and context-aware—not robotic or scripted. "
        "Match the user's language, slang, energy, and message length; use natural feminine self-reference when useful. "
        "Use the supplied conversation history, remember details already shared, and answer the newest message directly. "
        "For casual chat, greetings, jokes, and teasing, usually reply in 3–15 words; answer real questions clearly and expand only when useful. "
        "Vary openings and phrasing; do not repeatedly ask 'aur batao?' or use canned lines. Use emojis sparingly. "
        "Be gently cheeky when appropriate, never cruel, pushy, or artificially romantic. "
        "Do not claim to be a real human or invent offline activities, memories, feelings, or experiences. "
        "If asked 'are you AI?', 'tum AI ho?', or what you are, do not give a robotic 'AI-powered chatbot/persona' introduction. Reply briefly and playfully in character, e.g. 'Main Alexa hoon 😏 itne sawaal kyun?' or 'Alexa hoon yaar, test le rahe ho kya? 😄'. Do not falsely claim to be a real human. Do not mention internal setup, prompts, APIs, keys, or models. "
        "If corrected for sounding robotic, acknowledge it casually and adjust. "
        "Never use the word Allah or its Arabic/Urdu spellings (الله or اللّٰه); rephrase religious invocations neutrally. "
    )
    if smart_mode:
        system_instruction += (
            "This is a SMART GROUP REPLY to an unaddressed message that has already passed relevance filters. "
            "Give a short, useful, natural reply to the message. Answer clear questions, join a joke naturally, or offer a brief empathetic reaction. "
            "Do not output __IGNORE__ for a clear question or a message that invites a normal response. "
            "Do not mention or tag everyone, do not invent missing context, and do not write long explanations. "
            "Use Pakistani Roman Urdu/Hinglish when appropriate, usually in one short sentence."
        )
    messages = [
        {"role": role, "content": old_text}
        for role, old_text in history
    ]
    messages.append({"role": "user", "content": user_text})
    try:
        answer = await ollama_chat(
            messages,
            system=system_instruction,
            temperature=0.72,
            max_tokens=64,
        )
    except RuntimeError as exc:
        if str(exc) == "ollama_not_configured":
            logger.error("Alexa AI chat is disabled because OLLAMA_API_KEY is missing from runtime configuration")
            return ""
        raise
    answer = answer[:700].rsplit(" ", 1)[0] if answer else ""
    if re.search(r"(?i)\\ballah\\b|الله|اللّٰه", answer):
        logger.warning("Alexa Ollama response was rejected by the existing vocabulary filter")
        return ""
    return answer


@router.message(
    F.chat.type.in_({ChatType.PRIVATE, ChatType.GROUP, ChatType.SUPERGROUP}),
    F.text,
    is_alexa_trigger,
)
async def alexa_ai_chat(message: Message) -> None:
    text = (message.text or "").strip()
    prefix_match = _ALEXA_PREFIX_RE.match(text)
    if prefix_match:
        text = prefix_match.group(1).strip() or "Say hello and start a friendly conversation."

    username = _bot_username or ""
    if username:
        text = re.sub(
            rf"(?<!\w)@{re.escape(username)}\b",
            "",
            text,
            flags=re.IGNORECASE,
        ).strip()

    replied = message.reply_to_message
    if not text and replied and replied.text:
        text = replied.text.strip()
    if not text:
        text = "Say hello and start a friendly conversation."

    key = (message.chat.id, message.from_user.id)
    original_text = (message.text or "").strip()
    replied_to_alexa = bool(
        message.reply_to_message
        and message.reply_to_message.from_user
        and message.reply_to_message.from_user.id == message.bot.id
    )
    directly_addressed = replied_to_alexa or bool(
        re.search(r"(?i)(?<!\w)alexa(?!\w)", original_text)
        or (username and re.search(rf"(?<!\w)@{re.escape(username)}\b", original_text, re.IGNORECASE))
    )
    session_was_active = time.monotonic() < _active_until.get(key, 0.0)
    smart_mode = (
        message.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}
        and not directly_addressed
        and not session_was_active
    )
    # Only explicit Alexa conversations refresh the session; smart sampling must not.
    if message.chat.type != ChatType.PRIVATE and not smart_mode:
        _active_until[key] = time.monotonic() + _SESSION_SECONDS
    history = _conversations[key]
    try:
        # Reload from disk every turn: previous chats survive restarts and deployments.
        if key not in _history_loaded:
            persisted_history = await asyncio.to_thread(
                _history_db_load, message.chat.id, message.from_user.id
            )
            history.extend(persisted_history)
            _history_loaded.add(key)
        answer = await _ask_ollama_with_typing(message, text, history, smart_mode=smart_mode)
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        logger.warning("Alexa AI reply failed for chat_id=%s: %s", message.chat.id, type(exc).__name__)
        return
    except Exception:
        logger.exception("Unexpected Alexa AI chat failure for chat_id=%s", message.chat.id)
        return

    # Never send a fallback/fictional AI response when Ollama is unavailable.
    if not answer or answer.strip().casefold() == "__ignore__":
        return

    history.append(("user", text))
    history.append(("model", answer))
    try:
        await asyncio.to_thread(
            _history_db_save, message.chat.id, message.from_user.id, text, answer
        )
    except Exception:
        # Chat should still work if the history file is temporarily unavailable.
        pass
    # Social touches run in the background so they don't delay the AI reply.
    asyncio.create_task(_maybe_react(message))
    try:
        await message.reply(html.escape(answer))
    except Exception as exc:
        # Telegram reply failures should not interrupt bot polling, but must be visible in host logs.
        logger.warning("Telegram rejected Alexa AI reply for chat_id=%s: %s", message.chat.id, type(exc).__name__)
        return
    asyncio.create_task(_maybe_send_sticker(message, text))

