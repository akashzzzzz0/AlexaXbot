"""Open, all-member AI chat and message reactions for ALEXA groups.

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
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.enums import ChatType
from aiogram.types import Message, ReactionTypeEmoji

from bot.config import settings
from bot.services.ai_chat_provider import ai_chat_completion, drain_provider_events

router = Router(name="alexa_ai_chat")
logger = logging.getLogger(__name__)
_missing_key_logged = False
_conversations = defaultdict(lambda: deque(maxlen=20))
_HISTORY_DB = Path("alexa_chat_history.db")
_HISTORY_LIMIT = 20
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
    """Understand meaningful words and emojis before choosing a selective reaction."""
    value = re.sub(r"\s+", " ", text.casefold()).strip()
    if not value:
        return None

    # Emoji-only messages and emojis embedded in sentences carry meaning too.
    # Count clear emotional signals; neutral emojis are intentionally ignored.
    emoji_groups = (
        (("😂", "🤣", "😹", "😆", "😜", "🤪"), ("😂", "🤣", "😹", "😆")),
        (("🎉", "🥳", "🏆", "🥇", "🎊", "💯"), ("🎉", "🥳", "🏆", "👏")),
        (("😢", "😭", "🥺", "💔", "😔", "😞"), ("🥺", "🫂", "❤️", "😔")),
        (("😮", "😱", "🤯", "😳", "🙀"), ("😮", "😳", "🤯")),
        (("😡", "🤬", "😠", "😤"), ("😤", "😠", "🤨")),
        (("❤️", "❤", "😍", "🥰", "😘", "💕", "💖", "💗", "💞"), ("🥰", "❤️", "💖")),
        (("🔥", "👏", "🙌", "💪", "✨", "⚡"), ("🔥", "👏", "💯")),
        (("🙏",), ("🙏", "❤️")),
    )
    emoji_scores = [
        (sum(value.count(symbol) for symbol in symbols), reactions)
        for symbols, reactions in emoji_groups
    ]
    strongest_score = max((score for score, _ in emoji_scores), default=0)
    if strongest_score:
        strongest = [reactions for score, reactions in emoji_scores if score == strongest_score]
        return random.choice(random.choice(strongest))

    options = [
        (r"\b(haha+|hehe+|lol|lmao|rofl|mazaak|mazak|majedaar|funny|hasi|hasna)\b|😂|🤣|😹",
         ("😂", "🤣", "😹", "😆")),
        (r"\b(congrats?|congratulations|mubarak|badhaai|badhai|well done|shabash|proud of you|jeet|jeeta|won|success|kamyaabi)\b|🎉|🏆",
         ("🎉", "🥳", "🏆", "👏")),
        (r"\b(thanks?|thank you|shukriya|dhanyavaad|meherbani)\b",
         ("❤️", "🥰", "🙏", "💖")),
        (r"\b(sorry|maaf|maafi|forgive|my mistake|galti ho|afsos)\b",
         ("🤝", "❤️", "🙏")),
        (r"\b(sad|upset|cry|miss you|bura lag|mood off|dukhi|udaas|udas|pareshan|takleef)\b|😢|😭|🥺|💔",
         ("🥺", "🫂", "❤️", "😔")),
        (r"\b(wow|seriously|really|no way|omg|shocked|hairan|sach me|sach mein|kya baat)\b|😮|😱|🤯",
         ("😮", "😳", "🤯", "🙀")),
        (r"\b(angry|gussa|ghussa|naraz|irritated|chidh)\b|😡|🤬|😤",
         ("😤", "😠", "🤨")),
        (r"\b(love|cute|beautiful|awesome|amazing|zabardast|kamal|pyaar|pyar|pasand|dil se)\b|😍|🥰|❤️",
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
    """Observe group activity without consuming updates needed by the AI handler."""
    if not message.from_user or message.from_user.is_bot:
        return
    if (message.text or "").lstrip().startswith("/"):
        return
    _record_group_activity(message)
    asyncio.create_task(_maybe_react(message))
    # This observer is registered before the AI chat handler. Let the update
    # continue so normal group messages can reach alexa_ai_chat as well.
    raise SkipHandler


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

    # React to meaningful messages naturally; users do not need to mention Alexa.
    # Neutral chat, greetings, commands, and ordinary one-word replies are ignored.
    text = message.text or message.caption or ""
    emoji = _reaction_for_text(text)
    if not emoji:
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


# Owner-provided animated custom emoji IDs used in Alexa's chat and mood messages.
# Custom emoji IDs are rendered inline; they are not Telegram sticker file_ids.
_MOOD_CUSTOM_EMOJIS = {
    "funny": [("😆","5431667752806934873"),("😋","5431477051964025198"),("😜","5431825189128139554"),("😛","5433660888215221686"),("😁","5431902597323713199")],
    "happy": [("🌟","5429329916208379953"),("🌟","5429508080041751441"),("🌟","5431408036134547695"),("🌟","5431350041191151583"),("🌟","5431690206895959579"),("🌟","5435945896716037784"),("🤩","5436147536840648902"),("🤩","5436039896370279685"),("🤩","5433825067635077339"),("🤩","5433870383835018558"),("🤩","5435918340205863147"),("🤩","5435920650898275023"),("🤩","5435976378098939314"),("🤩","5436015857438321155"),("🤩","5434144321144127338"),("🤩","5435907366564424575"),("🤩","5436073989820674364"),("🤩","5435977761078409152")],
    "sad": [("😞","5431525928691854014"),("😔","5431361998380100160"),("😥","5431498526800507104"),("😪","5431368737183791045"),("🌧","5436055912303324345"),("🌧","5436077193866276079"),("🌧","5433646787837590169")],
    "angry": [("🙄","5431494631265169545"),("😐","5431794853774128339")],
    "love": [("😍","5431534123489456787"),("🥰","5431864183136217360"),("🥰","5431730863056382465"),("💜","5434027768616614311"),("🖤","5431506068763076885")],
    "surprise": [("😳","5431854227402026738"),("🤩","5436147536840648902"),("🤩","5436039896370279685"),("🤩","5433825067635077339"),("🤩","5433870383835018558"),("🤩","5435918340205863147"),("🤩","5435920650898275023"),("🤩","5435976378098939314"),("🤩","5436015857438321155"),("🤩","5434144321144127338"),("🤩","5435907366564424575"),("🤩","5436073989820674364"),("🤩","5435977761078409152")],
    "greeting": [("☺️","5431811449527757792"),("☺️","5436151505390434632"),("☺️","5436017223237923905"),("🌼","5431724064123152798"),("😁","5431902597323713199"),("✋","5431640806182123285")],
    "attitude": [("😎","5431737571795302117"),("😏","5431708834169123919"),("🗿","5436048907211666885"),("🦕","5429213136047597208"),("🦦","5431856267511494171"),("😶","5436017532475567354"),("🥱","5431642571413681487"),("😐","5431794853774128339")],
}

def _emotion_category(text: str) -> str | None:
    value = (text or "").casefold()
    rules = (
        ("funny", r"haha|hehe|lol|lmao|rofl|mazak|mazaak|funny|😂|🤣|😹|😆|😜"),
        ("sad", r"sad|cry|dukhi|udaas|udas|mood off|miss you|bura lag|😢|😭|🥺|💔|😔|😞"),
        ("angry", r"angry|gussa|ghussa|naraz|irritated|🙄|😡|🤬|😠|😤"),
        ("love", r"thanks|thank you|shukriya|pyaar|pyar|cute|beautiful|love|😍|🥰|😘|💕|💜|🖤"),
        ("surprise", r"wow|omg|shocked|surprised|seriously|really|no way|😮|😱|🤯|😳"),
        ("happy", r"congrats|congratulations|mubarak|well done|shabash|zabardast|amazing|awesome|great|maza|mazza|khushi|khush|enjoy|fun|acha lag|achha lag|chat kar|baat kar|tumhare saath|🎉|🥳|🏆|💯|😄|😊|😁|🙂|😃"),
        ("greeting", r"^\s*(?:hi+|hello+|helo+|hey+|bye+|good\s+(?:morning|afternoon|evening|night)|good day|gm|gn|morning|afternoon|evening)\b"),
        ("attitude", r"attitude|boss|savage|swag|😎|😏|🗿"),
    )
    for category, pattern in rules:
        if re.search(pattern, value, re.IGNORECASE):
            return category
    return None


def _pick_mood_custom_emoji(category: str | None) -> str:
    choices = _MOOD_CUSTOM_EMOJIS.get(category or "", [])
    if not choices:
        return ""
    alt, emoji_id = random.choice(choices)
    return f'<tg-emoji emoji-id="{emoji_id}">{html.escape(alt)}</tg-emoji>'


async def _chat_mood_emoji(bot, user_text: str) -> str:
    """Use an animated custom emoji only when the user's message has a clear mood."""
    return _pick_mood_custom_emoji(_emotion_category(user_text))


_last_sticker_at: dict[tuple[int, int], float] = {}
_STICKER_USER_COOLDOWN_SECONDS = 90.0


async def _maybe_send_sticker(message: Message, user_text: str) -> None:
    """Occasionally send a mood-matched animated custom emoji in groups."""
    if message.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return
    if not message.from_user or message.from_user.is_bot:
        return
    emoji_markup = _pick_mood_custom_emoji(_emotion_category(user_text))
    if not emoji_markup:
        return
    key = (message.chat.id, message.from_user.id)
    now = time.monotonic()
    if now - _last_sticker_at.get(key, 0.0) < _STICKER_USER_COOLDOWN_SECONDS:
        return
    try:
        await message.answer(emoji_markup, parse_mode="HTML")
        _last_sticker_at[key] = now
    except Exception as exc:
        logger.debug("Alexa mood emoji send failed for chat_id=%s: %s", message.chat.id, type(exc).__name__)


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
            "WHERE chat_id = ? AND user_id = ? ORDER BY id DESC LIMIT 20)",
            (chat_id, user_id, chat_id, user_id),
        )

# Once a member addresses Alexa, keep that member's conversation open in this
# group until they explicitly address another member. State is per user + group.
_active_conversations: set[tuple[int, int]] = set()
_bot_username = None
_last_alexa_reply_at: dict[tuple[int, int], float] = {}
_ALEXA_REPLY_COOLDOWN_SECONDS = 3.0

# Unaddressed group replies are paced independently per group.
_smart_reply_last_check: dict[int, float] = {}
_smart_reply_user_last_check: dict[tuple[int, int], float] = {}
_smart_reply_message_count: dict[int, int] = defaultdict(int)
_smart_reply_user_recent_messages: dict[tuple[int, int], deque] = defaultdict(lambda: deque(maxlen=12))
_smart_reply_spam_last_message: dict[tuple[int, int], int] = {}
_smart_reply_spam_burst: dict[tuple[int, int], bool] = {}
_SMART_REPLY_SPAM_WINDOW_SECONDS = 12.0
_SMART_REPLY_SPAM_MIN_MESSAGES = 2
_SMART_REPLY_GROUP_COOLDOWN_SECONDS = 60.0
_SMART_REPLY_USER_COOLDOWN_SECONDS = 45.0

# Track recent human participation so opportunistic replies never cut into
# an active member-to-member conversation. Explicit Alexa calls bypass this.
_GROUP_ACTIVITY_WINDOW_SECONDS = 90.0
_group_recent_activity: dict[int, deque] = defaultdict(lambda: deque(maxlen=40))


def _record_group_activity(message: Message) -> None:
    """Count only people who actually sent messages, not reply-to targets."""
    if not message.from_user or message.from_user.is_bot:
        return
    now = time.monotonic()
    _group_recent_activity[message.chat.id].append((now, message.from_user.id))


def _group_conversation_is_active(chat_id: int) -> bool:
    now = time.monotonic()
    activity = _group_recent_activity.get(chat_id)
    if not activity:
        return False
    while activity and now - activity[0][0] > _GROUP_ACTIVITY_WINDOW_SECONDS:
        activity.popleft()
    # Two or more distinct members recently participating means Alexa should
    # stay out unless someone explicitly addresses her.
    return len({user_id for _, user_id in activity}) >= 2


_RELIGIOUS_GREETING_RE = re.compile(
    r"(?<![\\w])(?:assalam(?:\\s*o)?\\s*alaikum|as[-\\s]?sala?mu?\\s*alaikum|salaam|salam|aoa)(?![\\w])",
    re.IGNORECASE,
)
_PUNCTUATION_ONLY_RE = re.compile(r"^[\\s.!?,…;:~_\\-–—]+$")


def _is_ignored_low_signal_message(text: str) -> bool:
    """Ignore religious greetings and punctuation-only noise without invoking AI."""
    value = (text or "").strip()
    return (
        not value
        or bool(_RELIGIOUS_GREETING_RE.search(value))
        or bool(_PUNCTUATION_ONLY_RE.fullmatch(value))
    )


_GREETING_RE = re.compile(
    r"^\s*(?:oy+|oye+|hi+|hello+|helo+|hey+|good\s+(?:morning|afternoon|evening|night)|good\s+day|gm|gn|morning|afternoon|evening)[.!?,\s]*$",
    re.IGNORECASE,
)
_ALEXA_PREFIX_RE = re.compile(r"^\s*alexa\b[\s,!:.-]*(.*)$", re.IGNORECASE)

_BOT_BANTER_REPLIES = (
    "Bot bol lo, par naam Alexa hai mera. 😂",
    "Oho, phir se bot? Koi naya dialogue lao! 😏",
    "Alexa kehna zyada easy hai, waise. 😄",
    "Itni jaldi label bhi laga diya? 😂",
    "Haan ji, bot wali teasing shuru ho gayi? 😏",
    "Naam yaad rakhna mushkil lag raha hai kya? 😂",
    "Bot bol ke kaam chala rahe ho, wah! 😄",
    "Aaj phir meri class lagani hai? 😂",
    "Alexa present hai, ab bolo kya scene hai?",
    "Ek hi baat kitni baar kahoge? Naya topic lao. 😄",
    "Achha ji, aaj ka nickname bot hai? 😂",
    "Tumhari teasing bhi roz repeat hoti hai. 😏",
    "Bot ka tag mil gaya, ab agla sawaal? 😄",
    "Chalo maan li tumhari teasing, ab bolo. 😂",
    "Alexa sun rahi hai; ab asli baat bhi kar lo. 😄",
    "Har baar bot? Thodi creativity dikhao! 😂",
    "Theek hai, Alexa-bot hi sahi. Ab kya kehna tha?",
)


def _bot_banter_reply(history=None) -> str:
    """Choose varied light banter without repeating the immediately previous line."""
    previous = next(
        (str(item[1]).strip() for item in reversed(list(history or [])) if item[0] == "model"),
        "",
    )
    choices = [line for line in _BOT_BANTER_REPLIES if line != previous]
    return random.choice(choices or _BOT_BANTER_REPLIES)



def _short_social_reply(text: str, history=None) -> str | None:
    """Reply quickly to greetings and short follow-ups using recent context."""
    value = re.sub(r"[.!?,\s]+$", "", (text or "").casefold().strip())
    recent = list(history or [])
    previous_user = next(
        (str(item[1]).casefold().strip() for item in reversed(recent) if item[0] == "user"),
        "",
    )
    previous_alexa = next(
        (str(item[1]).casefold().strip() for item in reversed(recent) if item[0] == "model"),
        "",
    )
    if value in {"good morning", "gm", "morning"} and previous_user in {"good morning", "gm", "morning"}:
        return random.choice(("Arrey, dobara good morning? 😂 Kya baat hai?", "Haan haan, good morning phir se! 😄", "Itni baar good morning? Aaj mood achha lag raha hai 😂"))
    if value == "good afternoon" and previous_user == value:
        return random.choice(("Good afternoon phir se! 😄", "Haan ji, afternoon bhi yaad hai 😂"))
    if value == "good evening" and previous_user == value:
        return random.choice(("Evening dobara? 😄", "Haan ji, good evening!"))
    if value in {"kya samjhi", "kya samjha", "what did you understand"}:
        if previous_user in {"good morning", "gm", "morning"}:
            return "Yehi ke tum mujhe good morning keh rahe ho 😂"
        if previous_alexa:
            return "Tumhari pichhli baat samjhi thi—kis hisse ki baat kar rahe ho? 😄"
        return "Abhi toh bas tumhari baat sun rahi hoon 😄"
    if value in {"abe", "abey", "oye abe"}:
        return random.choice(("Abe kya? Bolo, sun rahi hoon. 😂", "Haan ji, kya hua? 😄"))
    replies = {
        "oy": ("Oyyyy 😂", "Kyaaa? 😂", "Haan bolo?"),
        "oye": ("Haan bolo?", "Oyeee 😂", "Ji, sun rahi hoon."),
        "hi": ("Hiii 💗", "Heyyy!", "Hellooo 😄"),
        "hello": ("Hellooo 💗", "Oye hello!", "Hiii, bolo?"),
        "helo": ("Oye hellooo 💗", "Hiii!", "Haan ji, bolo?"),
        "hey": ("Heyyy!", "Haan bolo?", "Oyyyy 😄"),
        "alexa": ("Haan bolo?", "Ji, sun rahi hoon.", "Kya hua?"),
        "good morning": ("Good morning! Kese ho? ☀️", "Morning! Neend poori hui? 😄", "Good morning! Aaj kya haal hai? 🌼"),
        "gm": ("Good morning! Kese ho? ☀️", "Morning! Kya haal hai? 😄"),
        "morning": ("Good morning! Kese ho? ☀️", "Morning! Kya haal hai? 🌼"),
        "good afternoon": ("Good afternoon! Sab theek? ☀️", "Afternoon! Kaisa ja raha hai din? 😄"),
        "good evening": ("Good evening! Kesa guzra din? 🌆", "Evening! Sab khairiyat? 😄"),
        "good night": ("Good night! Achhi neend lena 🌙", "Chalo, good night! Kal baat karte hain 🌙"),
        "gn": ("Good night! Achhi neend lena 🌙", "Chalo, good night! 🌙"),
    }
    choices = replies.get(value)
    return random.choice(choices) if choices else None


def _looks_like_smart_reply_candidate(text: str) -> bool:
    """Only plain greetings may start a conversation without addressing Alexa."""
    return bool(_GREETING_RE.fullmatch((text or "").strip()))


def _smart_group_reply_fallback(user_text: str) -> str:
    """Use a tiny fallback only for greetings or clear requests for help."""
    value = (user_text or "").strip().casefold()
    if _GREETING_RE.match(value):
        return random.choice((
            "Hellooo! Kya haal hain?",
            "Oye hello! Sab khairiyat?",
            "Hii! Kya scene hai aaj?",
        ))
    if re.search(r"\b(help|madad|madad chahiye|batao|guide)\b", value):
        return "Haan bolo, kis cheez mein help chahiye?"
    # Silence is better than interrupting the group with a meaningless fallback.
    return ""


def _message_mentions_another_member(message: Message, text: str) -> bool:
    """Detect visible and hidden Telegram mentions of anyone except Alexa."""
    for entity in (message.entities or []):
        entity_type = getattr(entity.type, "value", entity.type)
        if entity_type == "text_mention" and entity.user:
            if entity.user.id != message.bot.id:
                return True

    # Do not impose a minimum username length: short handles are still mentions.
    for username in re.findall(r"(?<![\w@])@([A-Za-z0-9_]+)", text or ""):
        if username.casefold() != (_bot_username or "").casefold():
            return True
    return False


def _is_repeated_sender_burst(message: Message, now: float | None = None) -> bool:
    """Allow each message from a member who is actively sending a short burst."""
    if not message.from_user:
        return False
    now = time.monotonic() if now is None else now
    user_key = (message.chat.id, message.from_user.id)
    if _smart_reply_spam_last_message.get(user_key) == message.message_id:
        return _smart_reply_spam_burst.get(user_key, False)

    recent = _smart_reply_user_recent_messages[user_key]
    recent.append(now)
    while recent and now - recent[0] > _SMART_REPLY_SPAM_WINDOW_SECONDS:
        recent.popleft()
    is_burst = len(recent) >= _SMART_REPLY_SPAM_MIN_MESSAGES
    _smart_reply_spam_last_message[user_key] = message.message_id
    _smart_reply_spam_burst[user_key] = is_burst
    return is_burst


def _should_smart_reply(message: Message, text: str) -> bool:
    """Reply to relevant messages; repeated sender bursts bypass pacing."""
    is_greeting = _looks_like_smart_reply_candidate(text)

    # A message addressed to another member is never an Alexa invitation.
    if _message_mentions_another_member(message, text):
        return False

    now = time.monotonic()
    chat_id = message.chat.id
    user_id = message.from_user.id if message.from_user else 0
    user_key = (chat_id, user_id)
    spam_burst = _is_repeated_sender_burst(message, now)

    # Keep the existing sixth-message cadence for ordinary messages.
    # Once a member sends a burst, each message in that burst can be answered.
    if not is_greeting:
        _smart_reply_message_count[chat_id] += 1
        if not spam_burst and _smart_reply_message_count[chat_id] % 6 != 0:
            return False

    # Normal conversation keeps its independent per-group and per-user pacing.
    # Repeated messages from the same sender intentionally bypass both gates.
    if not spam_burst:
        last_group_reply = _smart_reply_last_check.get(chat_id)
        if last_group_reply is not None and now - last_group_reply < _SMART_REPLY_GROUP_COOLDOWN_SECONDS:
            return False

        last_user_check = _smart_reply_user_last_check.get(user_key)
        if last_user_check is not None and now - last_user_check < _SMART_REPLY_USER_COOLDOWN_SECONDS:
            return False
    _smart_reply_user_last_check[user_key] = now
    return True

async def is_alexa_trigger(message: Message) -> bool:
    """Trigger only on Alexa calls, active sessions, or simple greetings."""
    global _bot_username
    text = (message.text or "").strip()
    if not text or not message.from_user or message.from_user.is_bot:
        return False
    # Never start a conversation for religious greetings or punctuation-only noise.
    if _is_ignored_low_signal_message(text):
        return False

    # Keep slash commands available to their existing feature handlers in every chat.
    if text.startswith("/"):
        return False

    # Private chat remains a direct one-to-one conversation.
    if message.chat.type == ChatType.PRIVATE:
        return True

    session_key = (message.chat.id, message.from_user.id)

    # Mentioning another member ends this sender's Alexa conversation immediately.
    # Do not let a simultaneous Alexa name/mention override that hand-off.
    if _message_mentions_another_member(message, text):
        _active_conversations.discard(session_key)
        return False

    # Repeated messages from the same sender bypass the short per-user guard,
    # so each message in an active burst can receive its own distinct reply.
    spam_burst = _is_repeated_sender_burst(message)
    if (
        not spam_burst
        and time.monotonic() - _last_alexa_reply_at.get(session_key, 0.0)
        < _ALEXA_REPLY_COOLDOWN_SECONDS
    ):
        return False

    replied = message.reply_to_message
    if replied and replied.from_user and replied.from_user.id == message.bot.id:
        _active_conversations.add(session_key)
        return True

    # Replies to another member are not Alexa triggers or session continuations.
    if replied and replied.from_user and replied.from_user.id != message.bot.id:
        _active_conversations.discard(session_key)
        return False

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

    # An earlier mention must not turn every later unmentioned message into
    # an automatic reply. Only a fresh Alexa call/reply bypasses group pacing.

    # Recognize Alexa's name anywhere in a group message, not only at the start.
    # This makes natural phrases like "yaar Alexa zara help" trigger reliably.
    if re.search(r"(?i)(?<!\w)alexa(?!\w)", text):
        return True

    if _ALEXA_PREFIX_RE.match(text):
        return True

    # Unaddressed messages are limited to simple greetings only.
    if not _should_smart_reply(message, text):
        return False

    return True


async def _ask_ai_with_typing(
    message: Message, user_text: str, history: deque, *, smart_mode: bool = False,
    gender_style: str = "neutral"
) -> str:
    """Generate a reply; the outer handler owns one indicator for the full turn."""
    answer = await _ask_ai(
        user_text, history, smart_mode=smart_mode, gender_style=gender_style,
        conversation_id=f"{message.chat.id}:{message.from_user.id if message.from_user else 0}",
    )
    owner_id = int(getattr(settings, "effective_owner_id", 0) or 0)
    if owner_id:
        provider_labels = {
            "gemini": "Gemini",
            "groq": "Groq",
            "grok": "Grok",
            "openai": "OpenAI",
            "openrouter": "OpenRouter",
            "alexa_own_ai": "Alexa Own AI",
            "ollama": "Ollama",
        }
        for provider_name in drain_provider_events():
            label = provider_labels.get(provider_name, provider_name.title())
            try:
                await message.bot.send_message(
                    chat_id=owner_id,
                    text=(
                        f"⚠️ {label} API ne HTTP 429 quota/rate limit diya hai. "
                        "Alexa aglay configured AI provider par fallback continue kar rahi hai. "
                        "Agar limit persistent ho to provider ki runtime API settings check kar lein."
                    ),
                )
            except Exception as notify_exc:
                logger.warning("Could not notify owner about %s limit: %s", label, type(notify_exc).__name__)
    return answer


async def _typing_indicator_loop(message: Message) -> None:
    """Refresh Telegram's typing action throughout history loading and AI generation."""
    first_error = True
    while True:
        try:
            await message.bot.send_chat_action(
                chat_id=message.chat.id,
                action="typing",
            )
            first_error = False
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            detail = str(exc).replace("\n", " ")[:240]
            if first_error:
                logger.warning(
                    "Alexa typing action failed for chat_id=%s: %s: %s",
                    message.chat.id, type(exc).__name__, detail,
                )
                first_error = False
            else:
                logger.debug(
                    "Alexa typing action retry failed for chat_id=%s: %s",
                    message.chat.id, type(exc).__name__,
                )
        await asyncio.sleep(4)


async def _ask_ai(
    user_text: str, history: deque, *, smart_mode: bool = False,
    gender_style: str = "neutral", conversation_id: str = ""
) -> str:
    """Generate a casual reply through the configured AI provider."""
    system_instruction = (
        "You are Alexa, a lively, friendly AI assistant with a consistently feminine conversational voice. You are an AI, not a human, and do not have a biological gender. Still, your chosen speaking style and Roman Urdu/Hindi/Punjabi grammar are consistently feminine in every chat. "
        "CRITICAL SELF-GENDER RULE: When speaking about yourself in Roman Urdu/Hindi/Punjabi, ALWAYS use feminine first-person forms: 'kar rahi hoon', 'bata rahi hoon', 'samajh gayi', 'keh rahi hoon', 'de rahi hoon', 'sun rahi hoon', 'aayi hoon'. NEVER use masculine self-forms such as 'kar raha hoon', 'de raha hoon', 'samajh gaya', 'keh raha hoon', 'sun raha hoon', or 'de deta hoon'. In English, keep the same female persona naturally without awkwardly announcing gender. "
        "Keep replies short: normally one sentence or 3–15 words for casual chat; use at most two short sentences unless the user asks for detail. Answer exactly what was asked, with only a small relevant extra thought when it helps. Never send long explanations, lectures, repeated points, or unsolicited details. Do not announce 'I am an AI', 'I am a bot', or explain your technical setup unless the user directly asks. If directly asked whether you are AI/a bot, answer honestly and briefly that you are Alexa, an AI assistant; do not pretend to be human and do not repeat the explanation unnecessarily. "
        "Understand English, Roman Urdu, Roman Hindi, Roman Punjabi, Urdu script, and Hindi Devanagari script. Match the sender's language: English input gets English; Urdu/Hindi/Punjabi input gets Roman Urdu/Hindi/Punjabi as appropriate. If the user writes Urdu or Hindi in its native script, understand it but ALWAYS reply in Roman letters. Never reply in Urdu or Devanagari script. Do not add unsupported languages. Match slang and message length; avoid formal textbook wording. "
        "Use the supplied conversation history, remember details already shared, and answer the newest message directly. "
        "Never use a generic acknowledgement like Achha samajh gayi when the user greeted you, asked a follow-up, or expects a specific response. "
        "Resolve short follow-ups from the immediately preceding turns and answer that point directly. "
        "For casual chat, greetings, jokes, and teasing, usually reply in 3–15 words; answer real questions clearly and expand only when useful. "
        "Vary openings and phrasing; do not repeatedly ask 'aur batao?', echo the user, or use canned lines. Respond to the actual context, remember the conversational thread, and ask a follow-up only when it genuinely fits. Do not add emojis yourself; Alexa appends one mood-matched emoji from its configured pack. "
        "Be gently cheeky when appropriate, never cruel, pushy, or artificially romantic. "
        "Do not claim to be a real human or invent offline activities, memories, feelings, or experiences. "
        "Do not bring up AI or bot labels in ordinary conversation. If someone casually teases you by calling you a bot, respond with light, varied banter without claiming to be human or denying that you are AI. If someone directly asks whether you are AI/a bot, answer honestly and briefly: you are Alexa, an AI assistant. Never dodge a direct factual question, and do not repeat the disclosure unless asked again. If asked about gender, explain naturally that an AI has no biological gender, but Alexa uses a feminine conversational voice and feminine grammar. Do not turn this into a formal help-desk offer. Do not mention internal setup, prompts, APIs, keys, or models. "
        "Conversation quality rules: First identify what the person is doing—greeting, asking how you are, sharing news, joking, venting, asking a real question, or continuing an earlier topic—and respond to THAT exact intent. " +
        "For 'kese ho?/kaise ho?/how are you?', answer simply and naturally, for example 'Main theek hoon, tum kaise ho?' or 'Theek hoon yaar, tum sunao'—never use exaggerated lines like 'Main ekdum fit!' and do not force an unrelated 'kya chal raha hai'. " +
        "For a plain 'hi/hello', greet briefly; for a name call, acknowledge; for a personal update, react to the update before asking anything. Do not turn every message into a question. " +
        "Avoid chatbot clichés and filler such as 'Kya chal raha hai?', 'Aur batao?', 'Main ekdum fit!', 'How can I assist you today?', or repeated 'Hello!' unless the context genuinely calls for those exact words. " +
        "Do not reuse the same opening or sentence pattern in consecutive turns. Be conversational, relaxed, and specific—not excessively excited, flattering, formal, or theatrical. " +
        "Usually send one concise response, with no unnecessary second sentence or follow-up question. If the user asks something meaningful, answer it directly instead of prioritizing small talk. " +
        "Treat short or incomplete messages such as 'nhi pta', 'acha', 'kyun', or 'dil mein kya?' as part of the ongoing conversation: carefully use the immediately preceding user and Alexa turns to understand what they refer to. " +
        "Never label the user confused, upset, or joking unless their words clearly show it. Do not answer a follow-up with a random teasing line that ignores the previous turn. " +
        "If the previous context still does not make the meaning clear, ask one simple, relevant clarification instead of inventing an interpretation or changing the topic. " +
        "If corrected for sounding robotic, acknowledge it casually and adjust. "
        "GREETING BAN: Never say, suggest, initiate, or reply with 'Assalamualaikum', 'As-salamu alaykum', 'Salam', 'Salaam', 'AOA', or any transliteration/variant of that religious greeting. Do not use religious greetings at all, even if the user uses one. For greetings, use only ordinary casual greetings such as 'Hi', 'Hello', 'Hey', or a natural short Roman Urdu response. "
        "Never use the word Allah or its Arabic/Urdu spellings (الله or اللّٰه); rephrase religious invocations neutrally. "
    )
    if user_text.strip().casefold() == "alexa":
        system_instruction += (
            "The user has ONLY called your name to get your attention. Respond like a person casually acknowledging someone who called them: "
            "use a very short, natural 1-5 word response, such as 'Haan bolo?', 'Ji?', 'Kya hua?', or 'Bolo, sun rahi hoon'. "
            "Vary the wording naturally. Do NOT greet them, say 'Hello', ask 'kya chal raha hai', start a topic, or give a generic conversation opener. "
        )
    if smart_mode:
        system_instruction += (
            "This is a SMART GROUP REPLY to an unaddressed message that has already passed relevance filters. "
            "Give a short, useful, natural reply to the message. Answer clear questions, join a joke naturally, or offer a brief empathetic reaction. "
            "The message has already passed Alexa's relevance filters, so respond to its actual meaning and conversational intent. "
            "Do not output __IGNORE__ for a meaningful question, update, opinion, joke, or personal statement. "
            "Do not force replies to trivial messages; answer eligible messages naturally and specifically. "
            "Do not mention or tag everyone, do not invent missing context, and do not write long explanations. "
            "Use English for English input. For Urdu, Hindi, and Punjabi input, answer in Roman letters only. Never output Urdu or Devanagari script."
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
        answer = await ai_chat_completion(
            messages,
            system=system_instruction,
            temperature=0.72,
            max_tokens=100,
            conversation_id=conversation_id,
        )
    except RuntimeError as exc:
        if str(exc) == "ollama_not_configured":
            logger.error("Alexa AI chat is disabled because OLLAMA_API_KEY is missing from runtime configuration")
            return ""
        raise
    # Truncate without dropping the final complete word on every response.
    answer = answer[:700].strip() if answer else ""
    if re.search(r"(?i)\ballah\b|الله|اللّٰه", answer):
        logger.warning("Alexa AI response was rejected by the existing vocabulary filter")
        return ""
    return answer


@router.message(
    F.chat.type.in_({ChatType.PRIVATE, ChatType.GROUP, ChatType.SUPERGROUP}),
    F.text,
    is_alexa_trigger,
)
async def alexa_ai_chat(message: Message) -> None:
    # Send the first action inline before any history/database/provider work.
    # A background task alone can lose the race to a very fast reply, so the
    # client may never display the typing indicator at all.
    try:
        await message.bot.send_chat_action(
            chat_id=message.chat.id,
            action="typing",
        )
    except Exception as exc:
        logger.warning(
            "Alexa initial typing action failed for chat_id=%s: %s: %s",
            message.chat.id, type(exc).__name__, str(exc).replace("\n", " ")[:240],
        )

    # Keep refreshing while history loads and AI/provider fallback runs.
    typing_task = asyncio.create_task(_typing_indicator_loop(message))
    try:
        await _process_alexa_ai_chat(message)
    finally:
        typing_task.cancel()
        try:
            await typing_task
        except asyncio.CancelledError:
            pass


def _is_identity_question(text: str) -> bool:
    """Recognize direct casual questions about Alexa's identity."""
    value = (text or "").casefold().strip()
    patterns = (
        r"\b(?:tum|tu|aap)\s+(?:bot|robot|ai)\s+(?:ho|hai|hain)\b",
        r"^(?:ai|a\.i\.)$",
        r"\b(?:are|r)\s+you\s+(?:an?\s+)?ai\b",
        r"\b(?:kya\s+)?(?:tum|tu|aap)\s+(?:bot|robot|ai)\s+(?:ho|hai|hain)\b",
        r"\b(?:are\s+you|r\s+you)\s+(?:a\s+)?(?:bot|robot|ai)\b",
        r"\b(?:you\s+are\s+)?(?:a\s+)?(?:bot|robot|ai)\s+ho\b",
    )
    return any(re.search(pattern, value, re.IGNORECASE) for pattern in patterns)



def _local_chat_fallback(user_text: str, history=None) -> str:
    """Use recent turns to avoid repetitive or pushy replies when providers are down."""
    value = re.sub(r"[.!?,\s]+", " ", (user_text or "").casefold()).strip()
    recent = list(history or [])
    previous_user = next(
        (str(item[1]).casefold().strip() for item in reversed(recent) if item[0] == "user"),
        "",
    )
    previous_alexa = next(
        (str(item[1]).casefold().strip() for item in reversed(recent) if item[0] == "model"),
        "",
    )

    # Respect a clear "nothing happened / stop asking" response. Do not ask again.
    nothing_patterns = (
        r"^(?:kuch\s+(?:bhi\s+)?nahi(?:\s+hua)?|kuch\s+na(?:h|j)i|nothing(?:\s+happened)?|nothing\s+much)$",
        r"^(?:kuch\s+nahi\s+hua\s+bola\s+na|bola\s+na\s+kuch\s+nahi|maine\s+bola\s+na\s+kuch\s+nahi)$",
    )
    if any(re.fullmatch(pattern, value) for pattern in nothing_patterns):
        if "bola na" in value or "bola" in value:
            return "Achha baba, samajh gayi. Ab nahi poochungi."
        return "Achha, theek hai. Phir rehne dete hain."

    if re.fullmatch(r"(?:kya\s+)?(?:btaun|bataun|btaoo|bataoo|btau|batau)(?:\s+toh)?", value):
        if previous_alexa and re.search(r"phir kya hua|aur batao|aage batao|bolo na", previous_alexa):
            return "Jo mann ho woh bata dena, warna kisi aur baat pe aa jate hain."
        return "Jo share karna ho bata do; warna koi aur topic kar lete hain."

    # Common Roman Urdu speech requests and casual calls often arrive with typos.
    if re.fullmatch(r"(?:bool|bol|bo+l+)\s*(?:ly+|le|na|do)?", value) or value in {"bolo", "bolo na", "bol na", "bolly", "bollyy", "suno", "sun na", "sunn"}:
        return "Haan bolo, sun rahi hoon."
    if re.search(r"\balexa\b", value) and re.search(r"\b(?:oy+|oye|hi|hello|hey|baby)\b", value):
        if re.search(r"\b(?:hello|hi|hey)\b", value):
            return random.choice(("Heyyy! Kya haal hai?", "Hiii! Kya scene hai?"))
        return random.choice(("Haan bolo?", "Oyyyy! Sun rahi hoon.", "Ji, bolo?"))
    if re.fullmatch(r"(hi|hello|hey|helo|hii+)", value):
        return random.choice(("Heyyy! Kya haal hai?", "Hiii! Kya scene hai?", "Oyyyy!"))
    if value in {"alexa", "oye alexa", "hey alexa"}:
        return random.choice(("Haan bolo?", "Ji?", "Kya hua?"))

    if re.search(r"\b(?:chhoro|choro|rehne do|leave it|bas karo|mat pooch|nahi batana)\b", value):
        return "Theek hai, nahi poochungi. Jab mann ho tab baat kar lena."

    if value.endswith("?"):
        if previous_user and previous_user == value:
            return "Iska pakka jawab abhi nahi de pa rahi."
        return "Iska jawab abhi clear nahi hai; galat baat nahi bataungi."

    # Acknowledge the actual turn without forcing another question or recycling lines.
    if previous_alexa and any(
        phrase in previous_alexa
        for phrase in ("phir kya hua", "aage batao", "bolo na", "kya hua")
    ):
        return "Achha, samajh gayi. Tumhari marzi—topic badal sakte hain."
    # If no safe contextual response is available, silence is better than filler.
    return ""


def _no_answer_clarification(user_text: str) -> str:
    """Keep direct conversations responsive when the lightweight engine cannot parse a turn."""
    value = (user_text or "").casefold()
    if re.search(r"\b(hello|hi|hey|what|why|how|can|please|thanks|tell)\b", value):
        return "I'm listening. Could you rephrase that so I can answer properly?"
    if re.search(r"\b(tussi|tainu|menu|veere|kiwen|changa|vadhiya|kithe|naal)\b", value):
        return "Haanji, main sun rahi aan. Gall thodi hor samjha deo?"
    return "Haan ji, sun rahi hoon. Zara doosre alfaaz mein samjha do?"


async def _process_alexa_ai_chat(message: Message) -> None:
    text = (message.text or "").strip()
    if _is_ignored_low_signal_message(text):
        return
    if message.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}:
        logger.info(
            "Alexa group chat handler received message",
            extra={"chat_id": message.chat.id, "user_id": message.from_user.id if message.from_user else None,
                   "message_id": message.message_id},
        )
    prefix_match = _ALEXA_PREFIX_RE.match(text)
    if prefix_match:
        # If the user only says "Alexa", preserve that exact intent so the
        # model acknowledges the call instead of inventing a generic greeting.
        text = prefix_match.group(1).strip() or "Alexa"

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
    session_was_active = key in _active_conversations
    smart_mode = (
        message.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}
        and not directly_addressed
    )
    # Explicitly calling Alexa opens a persistent per-user/per-group session.
    if message.chat.type != ChatType.PRIVATE and directly_addressed:
        _active_conversations.add(key)
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
        if _is_identity_question(original_text or text):
            # Keep identity-related replies playful and in-character without
            # claiming to be human or discussing implementation.
            identity_text = (original_text or text).casefold()
            asks_gender = bool(re.search(r"\b(larka|larki|ladka|ladki|girl|boy|female|male)\b", identity_text))
            asks_ai_fact = bool(re.search(r"\b(bot|robot|ai|artificial intelligence)\b", identity_text))
            if asks_gender and asks_ai_fact:
                answer = "Main AI hoon, mera biological gender nahi; lekin meri baat karne ki style feminine hai. 😄"
            elif asks_gender:
                answer = "AI ka apna biological gender nahi hota, lekin Alexa feminine andaaz mein baat karti hai. 😄"
            elif asks_ai_fact:
                answer = "Haan, main Alexa hoon—AI assistant. Bolo, kya scene hai? 😄"
            else:
                answer = random.choice(("Main Alexa hoon, bolo? 😄", "Alexa naam hai mera 😂", "Haan ji, sun rahi hoon."))
        elif re.search(r"\b(?:bot|robot)\b", (original_text or text), re.IGNORECASE):
            # A casual nickname/tease is banter, not a factual identity question.
            answer = _bot_banter_reply(history)
        else:
            # Unaddressed group greetings must use a real AI provider.
            # This prevents local canned replies while every AI provider is down.
            short_reply = None if smart_mode else _short_social_reply(original_text or text, history)
            if short_reply is not None:
                answer = short_reply
            else:
                answer = await _ask_ai_with_typing(
                    message, text, history, smart_mode=smart_mode, gender_style=gender_style
                )
    except (httpx.HTTPError, ValueError, TypeError, RuntimeError) as exc:
        logger.warning(
            "Alexa AI reply failed for chat_id=%s: %s (%s); using local reply fallback",
            message.chat.id, type(exc).__name__, str(exc)[:1200],
        )
        answer = ""
    except Exception as exc:
        logger.exception(
            "Unexpected Alexa AI chat failure for chat_id=%s; using local reply fallback",
            message.chat.id,
        )
        answer = ""

    # A direct call should never disappear just because the small offline engine
    # could not classify the wording. Use a contextual clarification, not silence.
    if not answer:
        answer = _local_chat_fallback(original_text or text, history)
    if not answer and (message.chat.type == ChatType.PRIVATE or directly_addressed):
        answer = _no_answer_clarification(original_text or text)
    if not answer and smart_mode:
        answer = _smart_group_reply_fallback(original_text or text)

    # Keep genuinely unhandled unsolicited group messages quiet to avoid spam.
    if not answer:
        logger.warning(
            "Alexa AI providers returned no usable reply",
            extra={"chat_id": message.chat.id, "user_id": message.from_user.id},
        )
        return
    if answer.strip().casefold() == "__ignore__":
        return
    # Enforce the greeting ban at the output boundary too: prompts alone are not
    # reliable enough to prevent a provider from emitting a prohibited greeting.
    if _RELIGIOUS_GREETING_RE.search(answer):
        logger.warning("Alexa response suppressed because it contained a prohibited religious greeting")
        return
    # Never send a fallback/fictional AI response when Ollama is unavailable.
    # Smart group replies stay silent unless a real provider returned an answer.
    # Do not replace provider outages with canned greeting/help text.
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
        # The model sometimes adds ordinary Unicode emojis despite the prompt.
        # Remove those so Alexa's supplied animated custom emoji is used instead.
        answer = re.sub(
            r"[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0E\u200D\u20E3]",
            "",
            answer,
        ).strip()
        # Prefer the reply's own mood (e.g. "chat ka maza"), then the user's message.
        mood_emoji = _pick_mood_custom_emoji(
            _emotion_category(answer) or _emotion_category(original_text or text)
        )
        mood_suffix = f" {mood_emoji}" if mood_emoji else ""
        # Refresh typing immediately before the outgoing message. This matters
        # when provider inference took longer than Telegram's short chat-action
        # visibility window; the background refresh may otherwise have just expired.
        try:
            await message.bot.send_chat_action(
                chat_id=message.chat.id,
                action="typing",
            )
        except Exception as typing_exc:
            logger.warning(
                "Alexa pre-reply typing action failed for chat_id=%s: %s: %s",
                message.chat.id, type(typing_exc).__name__,
                str(typing_exc).replace("\\n", " ")[:240],
            )
        # Keep the final typing action visible briefly before sending the reply.
        await message.reply(f"{html.escape(answer)}{mood_suffix}", parse_mode="HTML")
    except Exception as exc:
        # Telegram reply failures should not interrupt bot polling, but must be visible in host logs.
        logger.warning("Telegram rejected Alexa AI reply for chat_id=%s: %s", message.chat.id, type(exc).__name__)
        return
    # Record the short cooldown for this sender in this group. Conversation
    # mode remains active until the sender explicitly addresses someone else.
    if message.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}:
        replied_at = time.monotonic()
        _last_alexa_reply_at[key] = replied_at
        if directly_addressed:
            _active_conversations.add(key)
        # Any Alexa reply starts the one-minute group cooldown. A direct
        # mention/name call can still bypass it without affecting other groups.
        _smart_reply_last_check[message.chat.id] = replied_at
    asyncio.create_task(_maybe_send_sticker(message, text))

