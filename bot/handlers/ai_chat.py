"""Open, all-member Ollama-backed chat and message reactions for ALEXA groups.

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
def _detect_user_gender_style(history, current_text: str) -> str:
    """Use only clear first-person self-identification; never guess from a name or ID."""
    girl_patterns = (
        r"\b(?:main|mai|mein|me)\s+(?:ek\s+)?(?:larki|ladki|girl|female)\s+(?:hoon|hun|hu|houn)\b",
        r"\b(?:i am|i'm)\s+(?:a\s+)?(?:girl|female|woman)\b",
    )
    boy_patterns = (
        r"\b(?:main|mai|mein|me)\s+(?:ek\s+)?(?:larka|ladka|boy|male)\s+(?:hoon|hun|hu|houn)\b",
        r"\b(?:i am|i'm)\s+(?:a\s+)?(?:boy|male|man)\b",
    )
    user_messages = [str(item_text) for role, item_text in history if role == "user"]
    user_messages.append(current_text)
    for value in reversed(user_messages[-30:]):
        normalized = value.casefold().strip()
        if any(re.search(pattern, normalized, re.IGNORECASE) for pattern in girl_patterns):
            return "girl"
        if any(re.search(pattern, normalized, re.IGNORECASE) for pattern in boy_patterns):
            return "boy"
    return "neutral"


def _reaction_for_text(text: str) -> str | None:
    """React only to clear, high-signal emotional or celebratory messages."""
    value = re.sub(r"\s+", " ", text.casefold()).strip()
    if not value:
        return None

    options = [
        (r"\b(haha+|hehe+|lol|lmao|rofl|mazaak|mazak|funny)\b|😂|🤣|😹",
         ("😂", "🤣", "😹", "😆")),
        (r"\b(congrats?|congratulations|mubarak|well done|shabash|proud of you|jeet|won)\b|🎉|🏆",
         ("🎉", "🥳", "🏆", "👏")),
        (r"\b(thanks?|thank you|shukriya|meherbani|jazakallah)\b",
         ("❤️", "🥰", "🙏", "💖")),
        (r"\b(sorry|maaf|forgive|my mistake|galti ho|afsos)\b",
         ("🤝", "❤️", "🙏")),
        (r"\b(sad|upset|cry|miss you|bura lag|mood off|dukhi|udaas|udas)\b|😢|😭|🥺|💔",
         ("🥺", "🫂", "❤️", "😔")),
        (r"\b(wow|seriously|really|no way|omg|shocked|hairan)\b|😮|😱|🤯",
         ("😮", "😳", "🤯", "🙀")),
        (r"\b(angry|gussa|ghussa|naraz|irritated)\b|😡|🤬|😤",
         ("😤", "😠", "🤨")),
        (r"\b(love|cute|beautiful|awesome|amazing|zabardast|kamal)\b|😍|🥰|❤️",
         ("🥰", "❤️", "✨", "🔥")),
    ]
    for pattern, emojis in options:
        if re.search(pattern, value, re.IGNORECASE):
            return random.choice(emojis)
    # No generic/neutral reaction: ordinary conversation stays reaction-free.
    return None


@router.message(
    F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
    F.text,
)
async def alexa_group_message_reactions(message: Message) -> None:
    """Only consider occasional reactions to messages explicitly mentioning Alexa."""
    if not message.from_user or message.from_user.is_bot:
        return
    if (message.text or "").lstrip().startswith("/"):
        return
    asyncio.create_task(_maybe_react(message))


async def _explicitly_mentions_alexa(message: Message) -> bool:
    """Require a real @Alexa mention; ordinary group chat never triggers reactions."""
    global _bot_username
    for entity in (message.entities or []):
        entity_type = getattr(entity.type, "value", entity.type)
        if entity_type == "text_mention" and entity.user and entity.user.id == message.bot.id:
            return True

    if _bot_username is None:
        try:
            me = await message.bot.get_me()
            _bot_username = (me.username or "").casefold()
        except Exception:
            _bot_username = ""
    if not _bot_username:
        return False

    text = message.text or ""
    return bool(re.search(
        rf"(?<![\\w@])@{re.escape(_bot_username)}(?![A-Za-z0-9_])",
        text,
        re.IGNORECASE,
    ))


_last_reaction_at: dict[tuple[int, int], float] = {}
_REACTION_USER_COOLDOWN_SECONDS = 45.0
_REACTION_CHANCE = 0.22


async def _maybe_react(message: Message) -> None:
    if not message.from_user or message.from_user.is_bot:
        return
    if not await _explicitly_mentions_alexa(message):
        return

    # Even a mention is not enough: only high-signal messages qualify, and
    # reactions remain occasional with a per-user cooldown.
    text = message.text or message.caption or ""
    emoji = _reaction_for_text(text)
    if not emoji or random.random() > _REACTION_CHANCE:
        return

    reaction_key = (message.chat.id, message.from_user.id)
    now = time.monotonic()
    if now - _last_reaction_at.get(reaction_key, 0.0) < _REACTION_USER_COOLDOWN_SECONDS:
        return
    _last_reaction_at[reaction_key] = now
    try:
        await message.bot.set_message_reaction(
            chat_id=message.chat.id,
            message_id=message.message_id,
            reaction=[ReactionTypeEmoji(emoji=emoji)],
        )
    except Exception as exc:
        logger.debug(
            "Alexa message reaction unavailable for chat_id=%s: %s",
            message.chat.id,
            type(exc).__name__,
        )


# Emotion-aware sticker selection from the public pack configured below.
_STICKER_SET_NAME = os.getenv("ALEXA_STICKER_SET_NAME", "video6104468803_by_vzrobot").strip()
_STICKER_SET_CACHE: list[tuple[str, str]] | None = None
_last_sticker_at: dict[tuple[int, int], float] = {}
_STICKER_USER_COOLDOWN_SECONDS = 90.0


def _emotion_category(text: str) -> str | None:
    value = text.casefold()
    rules = (
        ("funny", r"haha|hehe|lol|lmao|mazak|mazaak|😂|🤣|😹"),
        ("sad", r"sad|cry|dukhi|udaas|udas|mood off|😢|😭|🥺|💔"),
        ("angry", r"angry|gussa|ghussa|naraz|😡|🤬|😠|😤"),
        ("love", r"thanks|thank you|shukriya|pyaar|pyar|cute|❤️|😍|🥰|😘|💕"),
        ("surprise", r"wow|omg|shocked|surprised|😮|😱|🤯|😳"),
        ("greeting", r"^\s*(?:hi+|hello+|helo+|hey+|salam|assalamualaikum|aoa|bye+|good\s+(?:morning|afternoon|evening|night)|good day|gm|gn|morning|afternoon|evening)\b"),
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


# Custom emoji pack used inline in Alexa's actual chat replies.
_CUSTOM_EMOJI_SET_NAME = os.getenv(
    "ALEXA_CHAT_EMOJI_SET_NAME", "prem_28a5b_by_TgEmojis_bot"
).strip()
_CUSTOM_EMOJI_CACHE: list[tuple[str, str]] | None = None


async def _chat_mood_emoji(bot, user_text: str) -> str:
    """Choose one pack emoji from the sender's message mood; use Unicode if unavailable."""
    global _CUSTOM_EMOJI_CACHE
    category = _emotion_category(user_text) or "neutral"
    fallback_by_category = {
        "funny": "😂", "sad": "🥺", "angry": "😤", "love": "❤️",
        "surprise": "😮", "greeting": "😊", "attitude": "😎", "neutral": "🙂",
    }
    allowed = _EMOTION_EMOJIS.get(category, {"🙂", "😊", "😄", "✨"})
    if category == "neutral":
        allowed = {"🙂", "😊", "😄", "✨"}

    if _CUSTOM_EMOJI_CACHE is None:
        try:
            sticker_set = await bot.get_sticker_set(name=_CUSTOM_EMOJI_SET_NAME)
            _CUSTOM_EMOJI_CACHE = [
                (str(getattr(sticker, "emoji", "") or ""), str(getattr(sticker, "custom_emoji_id", "") or ""))
                for sticker in sticker_set.stickers
                if getattr(sticker, "custom_emoji_id", None)
            ]
        except Exception as exc:
            logger.warning("Alexa custom emoji pack unavailable: %s", type(exc).__name__)
            _CUSTOM_EMOJI_CACHE = []

    matches = [
        (alt, emoji_id) for alt, emoji_id in (_CUSTOM_EMOJI_CACHE or [])
        if alt in allowed and emoji_id.isdigit()
    ]
    if not matches and category == "neutral":
        matches = [
            (alt, emoji_id) for alt, emoji_id in (_CUSTOM_EMOJI_CACHE or [])
            if alt in {"🙂", "😊", "😄", "✨", "💬"} and emoji_id.isdigit()
        ]
    if matches:
        alt, emoji_id = random.choice(matches)
        return f'<tg-emoji emoji-id="{emoji_id}">{html.escape(alt)}</tg-emoji>'
    return fallback_by_category.get(category, "🙂")


async def _maybe_send_sticker(message: Message, user_text: str) -> None:
    """Send an occasional sticker matching the user's emotional cue."""
    global _STICKER_SET_CACHE
    category = _emotion_category(user_text)
    if not category or random.random() >= 0.50:
        return
    if not message.from_user or message.from_user.is_bot:
        return
    sticker_key = (message.chat.id, message.from_user.id)
    now = time.monotonic()
    if now - _last_sticker_at.get(sticker_key, 0.0) < _STICKER_USER_COOLDOWN_SECONDS:
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
            _last_sticker_at[sticker_key] = now
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
            _last_sticker_at[sticker_key] = now
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
    r"^\s*(?:hi+|hello+|helo+|hey+|salam|assalamualaikum|aoa|"
    r"good\s+(?:morning|afternoon|evening|night)|good\s+day|morning|afternoon|evening|gm|gn)(?:\b.*)?$",
    re.IGNORECASE,
)
_ALEXA_PREFIX_RE = re.compile(r"^\s*alexa\b[\s,!:.-]*(.*)$", re.IGNORECASE)


def _looks_like_smart_reply_candidate(text: str) -> bool:
    """Filter low-value group chatter before spending an Ollama request."""
    value = text.strip().casefold()
    # Common standalone greetings should get a reply even when they are short.
    if _GREETING_RE.match(value):
        return True
    if len(value) < 8:
        return False
    if re.fullmatch(r"(?:ok(?:ay)?|acha|hmm+|huh|nice|same|true|right|👍+|👌+|❤️+|😂+|🤣+|🙏+)[.!?\s]*", value):
        return False
    if re.search(r"[?؟]|\b(?:kya|kyun|kaise|kaisa|kaisi|kab|kahan|kon|kaun|who|what|why|how|when|where|which|anyone|suggest|opinion|batao|help)\b|😂|🤣|😹|😢|😭|😮|😱", value, re.IGNORECASE):
        return True
    # Longer conversational statements may be worth a response; the model decides.
    return len(value.split()) >= 7 or len(value) >= 45


def _smart_group_reply_fallback(user_text: str) -> str:
    """Never leave a relevant unaddressed group message unanswered when the model opts out."""
    value = (user_text or "").strip().casefold()
    if _GREETING_RE.match(value):
        return random.choice((
            "Hellooo! Kya haal hain?",
            "Oye hello! Sab khairiyat?",
            "Hii! Kya scene hai aaj?",
        ))
    if re.search(r"\b(help|madad|madad chahiye|batao|guide)\b", value):
        return "Haan bolo, kis cheez mein help chahiye?"
    return "Haan, bolo—kya baat hai?"


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
    """Trigger every ordinary human text message in groups and private chats."""
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
    if replied and replied.from_user and replied.from_user.id == message.bot.id:
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

    # Owner-requested open group chat: reply to every ordinary human text
    # message, even when Alexa is not tagged. Slash commands and messages
    # explicitly addressed to another tagged member were filtered above.
    return True


async def _ask_ollama_with_typing(
    message: Message, user_text: str, history: deque, *, smart_mode: bool = False,
    gender_style: str = "neutral"
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
        return await _ask_ollama(
            user_text, history, smart_mode=smart_mode, gender_style=gender_style
        )
    finally:
        typing_task.cancel()


async def _ask_ollama(
    user_text: str, history: deque, *, smart_mode: bool = False,
    gender_style: str = "neutral"
) -> str:
    """Generate a casual reply through the shared Ollama provider."""
    system_instruction = (
        "You are Alexa, a lively feminine Telegram chat persona fluent in Pakistani Roman Urdu/Hinglish. "
        "Sound spontaneous, warm, playful, socially aware, and context-aware—not robotic or scripted. "
        "Match the user's language, slang, energy, and message length; use natural feminine self-reference when useful. "
        "Use the supplied conversation history, remember details already shared, and answer the newest message directly. "
        "For casual chat, greetings, jokes, and teasing, usually reply in 3–15 words; answer real questions clearly and expand only when useful. "
        "Vary openings and phrasing; do not repeatedly ask 'aur batao?' or use canned lines. Do not add emojis yourself; Alexa appends one mood-matched emoji from its configured pack. "
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
            "Do not output __IGNORE__ for any ordinary human group message. Respond naturally even if it is a short statement, joke, greeting, or casual comment. "
            "Do not mention or tag everyone, do not invent missing context, and do not write long explanations. "
            "Use Pakistani Roman Urdu/Hinglish when appropriate, usually in one short sentence."
        )
    system_instruction += (
        "Keep Alexa's own persona feminine in every conversation. Adapt only the friendliness style to the user's clearly self-stated gender: "
        "for a girl, be extra caring, considerate, and warmly friendly; for a boy, stay normally friendly and casual. "
        "This is never romantic, flirtatious, possessive, or intimate. Do not use romantic pet names. "
        "Never infer gender from a name, username, profile photo, or writing style; if it is not clearly self-stated, use a neutral friendly tone. "
    )
    if gender_style == "girl":
        system_instruction += "The sender has clearly identified herself as a girl. Use a caring, considerate, warmly friendly tone, without romance or flirting. "
    elif gender_style == "boy":
        system_instruction += "The sender has clearly identified himself as a boy. Use a normal friendly, casual tone. "
    else:
        system_instruction += "The sender's gender is not clearly known. Keep the tone neutral and friendly; do not guess. "
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
    if message.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}:
        logger.info(
            "Alexa group chat handler received message",
            extra={"chat_id": message.chat.id, "user_id": message.from_user.id if message.from_user else None,
                   "message_id": message.message_id},
        )
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
        gender_style = _detect_user_gender_style(history, original_text or text)
        answer = await _ask_ollama_with_typing(
            message, text, history, smart_mode=smart_mode, gender_style=gender_style
        )
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        logger.warning("Alexa AI reply failed for chat_id=%s: %s", message.chat.id, type(exc).__name__)
        # Basic greetings should never be silently ignored just because Ollama is
        # temporarily unavailable. Keep this fallback limited to unaddressed group
        # greetings; addressed/private AI requests still fail silently as before.
        if smart_mode:
            answer = _smart_group_reply_fallback(original_text or text)
        else:
            return
    except Exception:
        logger.exception("Unexpected Alexa AI chat failure for chat_id=%s", message.chat.id)
        if smart_mode:
            answer = _smart_group_reply_fallback(original_text or text)
        else:
            return

    # Never send a fallback/fictional AI response when Ollama is unavailable.
    if smart_mode and (not answer or answer.strip().casefold() == "__ignore__"):
        answer = _smart_group_reply_fallback(original_text or text)
        logger.warning(
            "Alexa smart group reply used deterministic fallback",
            extra={"chat_id": message.chat.id, "user_id": message.from_user.id},
        )
    if not answer:
        logger.warning(
            "Alexa AI returned no usable reply",
            extra={"chat_id": message.chat.id, "user_id": message.from_user.id},
        )
        return
    if answer.strip().casefold() == "__ignore__":
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
    # Group reactions are handled independently for all suitable messages.
    try:
        mood_emoji = await _chat_mood_emoji(message.bot, original_text or text)
        await message.reply(f"{html.escape(answer)} {mood_emoji}", parse_mode="HTML")
    except Exception as exc:
        # Telegram reply failures should not interrupt bot polling, but must be visible in host logs.
        logger.warning("Telegram rejected Alexa AI reply for chat_id=%s: %s", message.chat.id, type(exc).__name__)
        return
    asyncio.create_task(_maybe_send_sticker(message, text))

