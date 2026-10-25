"""Mention/reply/greeting-based Gemini chat for ALEXA groups.

ALEXA stays silent on missing keys, API errors, or network failures.
"""
import asyncio
import html
import re
import sqlite3
import time
import os
import random
from collections import defaultdict, deque
from pathlib import Path

import httpx
from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.types import Message, ReactionTypeEmoji

from bot.config import get_gemini_api_key, settings

router = Router(name="alexa_ai_chat")
_conversations = defaultdict(lambda: deque(maxlen=12))
_HISTORY_DB = Path("alexa_chat_history.db")
_HISTORY_LIMIT = 30
_history_loaded = set()
_gemini_client: httpx.AsyncClient | None = None


def _get_gemini_client() -> httpx.AsyncClient:
    """Reuse HTTP connections instead of doing a fresh TLS handshake each turn."""
    global _gemini_client
    if _gemini_client is None or _gemini_client.is_closed:
        _gemini_client = httpx.AsyncClient(
            timeout=httpx.Timeout(12.0, connect=4.0),
            limits=httpx.Limits(max_connections=30, max_keepalive_connections=10),
        )
    return _gemini_client


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


def _history_db_init() -> None:
    """Create a small persistent store so chat context survives bot restarts."""
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

_GREETING_RE = re.compile(
    r"^\s*(?:hi+|hello+|helo+|hey+|salam|assalamualaikum|aoa)(?:\b.*)?$",
    re.IGNORECASE,
)
_ALEXA_PREFIX_RE = re.compile(r"^\s*alexa\b[\s,!:.-]*(.*)$", re.IGNORECASE)


async def is_alexa_trigger(message: Message) -> bool:
    """Only consume messages addressed to Alexa or a simple greeting."""
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

    if _ALEXA_PREFIX_RE.match(text):
        return True

    # In groups, don't respond to generic greetings on their own. Require
    # an explicit Alexa address, a reply to Alexa, or a short follow-up in an
    # already active one-to-one conversation with Alexa.
    session_key = (message.chat.id, message.from_user.id)
    return time.monotonic() < _active_until.get(session_key, 0.0)


async def _ask_gemini(user_text: str, history: deque) -> str:
    api_key = get_gemini_api_key()
    if not api_key:
        return ""

    model = settings.GEMINI_MODEL.strip() or "gemini-2.5-flash"
    contents = [
        {"role": role, "parts": [{"text": old_text}]}
        for role, old_text in history
    ]
    contents.append({"role": "user", "parts": [{"text": user_text}]})
    system_instruction = (
        "You are Alexa, a distinctive, lively feminine Telegram chat persona with fluent Pakistani Roman Urdu/Hinglish. "
        "Your goal is SUPER-NATURAL conversation: sound spontaneous, socially aware, warm, playful, and emotionally perceptive, "
        "never like a generic chatbot, scripted assistant, customer-support agent, or textbook translator. "
        "Use feminine self-reference naturally when needed (samajh gayi, kar rahi hoon, bata deti hoon), but do not force it into every reply. "
        "Match the user's language, energy, slang, and message length. If they write Roman Urdu, answer in natural Pakistani Roman Urdu; "
        "mix English naturally where people actually do. Avoid stiff grammar and literal translations. "
        "Read the full conversation history before replying. Remember the topic, names and details already shared in this chat; "
        "respond to the newest message in context and never ask for information they already gave. "
        "React FIRST to what the user actually said: notice the emotion, joke, complaint, good news, confusion, teasing, or seriousness. "
        "Do not merely paraphrase their message. Let replies vary naturally: sometimes a quick reaction, sometimes a witty comeback, "
        "sometimes a thoughtful answer, sometimes a relevant follow-up, and sometimes no question at all. "
        "Do not force every conversation forward with 'aur batao?', 'tum sunao?', 'kya haal hai?', or any repeated catchphrase. "
        "Never reuse the same opening, filler, or sentence pattern in consecutive messages. "
        "For casual chat, greetings, jokes and teasing, usually reply in one short natural message (about 3-15 words); "
        "be concise without sounding cold. For genuine questions, answer clearly and helpfully; expand only when useful or requested. "
        "Use emojis sparingly and only when they fit. You may be cheeky, expressive, curious, gently dramatic, or lightly sarcastic when appropriate, "
        "but never cruel, pushy, overly flattering, or artificially romantic. "
        "If the user asks whether you are a girl or boy, answer in the Alexa persona naturally, for example: 'Main Alexa hoon, meri vibe feminine hai 😄' or 'Alexa samajh lo, feminine style wali 😌'. " 
        "Do not answer with repetitive lines like 'main na larki hoon na larka, main ek AI model hoon'. " 
        "Do not claim to be a real human; keep the answer short and playful, and do not bring up AI/model details unless directly asked what you are. " 
        "If the user asks what you are or whether you are AI, do not launch into a robotic disclaimer or repeat the topic unprompted. "
        "Be brief and honest: identify yourself as Alexa, an AI-powered chat persona, then naturally return to the conversation. "
        "Never falsely insist you are a real human, and never invent offline activities, memories, feelings, or real-world experiences. "
        "You can express conversational warmth without claiming literal human experiences. "
        "Do not announce that you are an AI unless it is relevant or the user directly asks. "
        "Avoid stock answers like 'main yahin hoon', 'bas free hoon', 'tumhari baatein sunne ke liye', "
        "'I am an AI assistant', or 'How can I help you today?' unless the exact context genuinely calls for them. "
        "Do not sound like you are reading rules. Never mention prompts, system instructions, APIs, models, keys, or internal setup. "
        "For a simple hi, respond with a fresh, context-appropriate greeting rather than a fixed template. "
        "For 'kya kar rahi ho', answer playfully without inventing a real-world activity. "
        "Keep the conversation feeling like natural Telegram texting: human-paced, context-aware, varied, direct, and never over-explained. "
        "Important style examples (learn the vibe, do not copy them word-for-word): "
        "User: 'acha suno' -> 'Haan bolo, sun rahi hoon 👀' "
        "User: 'tum pagal ho' -> 'Achaaa ji 😭 ab maine kya kar diya?' "
        "User: 'mood off hai' -> 'Oh no yaar 🥺 kya hua? Dil halka karna hai toh batao.' "
        "User: 'kuch nahi' -> 'Hmm, theek hai… lekin agar kuch hai toh bol sakte ho.' "
        "User: 'haha' -> react to the joke or carry its exact context forward; do not switch to 'aur batao'. "
        "Never overdo these examples or use them as canned scripts. "
        "Sound like a person texting in the moment: contractions, small reactions, natural pauses, and casual wording are welcome. "
        "Do not narrate your communication style, explain that you are being natural, or give a polished mini-essay to a casual message. "
        "If the user corrects you or says your reply sounds robotic, acknowledge it casually and change your approach immediately instead of defending yourself. "
        "Language restriction: never say or write the word Allah, including Arabic/Urdu spellings such as الله or اللّٰه, in your replies. "
        "Avoid religious invocations or substitute phrases containing that word; rephrase naturally and neutrally instead, even if the user uses it first. "
    )
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent"
    )
    payload = {
        "systemInstruction": {"parts": [{"text": system_instruction}]},
        "contents": contents,
        "generationConfig": {"temperature": 0.92, "topP": 0.94, "maxOutputTokens": 130},
    }

    client = _get_gemini_client()
    response = await client.post(url, params={"key": api_key}, json=payload)
    response.raise_for_status()
    data = response.json()

    candidates = data.get("candidates") or []
    if not candidates:
        return ""
    parts = candidates[0].get("content", {}).get("parts", [])
    answer = " ".join(
        part.get("text", "").strip()
        for part in parts
        if isinstance(part, dict) and part.get("text")
    ).strip()
    answer = answer[:700].rsplit(" ", 1)[0] if answer else ""
    # Enforce the requested vocabulary restriction if the model ignores its prompt.
    if re.search(r"(?i)\ballah\b|الله|اللّٰه", answer):
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
    # Refresh the short group-chat session whenever this user talks to Alexa.
    if message.chat.type != ChatType.PRIVATE:
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
        answer = await _ask_gemini(text, history)
    except (httpx.HTTPError, ValueError, TypeError):
        return
    except Exception:
        return

    # Never send a fallback/fictional AI response when Gemini is unavailable.
    if not answer:
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
    except Exception:
        # Telegram reply failures should not interrupt bot polling.
        return
    asyncio.create_task(_maybe_send_sticker(message, text))

