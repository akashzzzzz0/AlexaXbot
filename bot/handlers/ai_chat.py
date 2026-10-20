"""Mention/reply/greeting-based female-personality AI chat for ALEXA groups."""
import re
from collections import defaultdict, deque

import httpx
from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.types import Message

from bot.config import settings

router = Router(name="alexa_ai_chat")

# Small in-memory context per group member; deliberately bounded.
_conversations = defaultdict(lambda: deque(maxlen=12))
_bot_username = None

_GREETING_RE = re.compile(
    r"^\s*(?:hi+|hello+|helo+|hey+|salam|assalamualaikum|aoa)"
    r"(?:\b.*)?$",
    re.IGNORECASE,
)
_ALEXA_PREFIX_RE = re.compile(r"^\s*alexa\b[\s,!:.-]*(.*)$", re.IGNORECASE)


async def is_alexa_trigger(message: Message) -> bool:
    """Only consume messages addressed to Alexa or simple greetings."""
    global _bot_username
    text = (message.text or "").strip()
    if not text or not message.from_user or message.from_user.is_bot:
        return False

    # Replying directly to Alexa is always a trigger.
    replied = message.reply_to_message
    if replied and replied.from_user and replied.from_user.id == message.bot.id:
        return True

    # "Alexa, ..." also works without Telegram's @mention autocomplete.
    if _ALEXA_PREFIX_RE.match(text):
        return True

    # Telegram @mention using the actual bot username.
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

    # Let Alexa greet back to ordinary short greetings too.
    return bool(_GREETING_RE.match(text))


async def _ask_gemini(user_text: str, history: deque) -> str:
    api_key = settings.GEMINI_API_KEY.strip()
    if not api_key:
        return ""

    model = settings.GEMINI_MODEL.strip() or "gemini-2.5-flash"
    contents = []
    for role, text in history:
        contents.append({"role": role, "parts": [{"text": text}]})
    contents.append({"role": "user", "parts": [{"text": user_text}]})

    system_instruction = (
        "You are Alexa, a friendly female-presenting AI chatbot in a Telegram group. "
        "Speak naturally and casually, usually in Roman Urdu/Hinglish matching the user. "
        "Use feminine Urdu phrasing for yourself where natural (for example, 'kar rahi hoon'). "
        "Be warm, playful, respectful, concise, and use occasional fitting emojis. "
        "Respond to greetings naturally, e.g. if someone says 'Alexa kese ho?' say "
        "'Main theek hoon 😊 Tum kese ho?'. Do not claim to be a real human. "
        "Answer the actual message; do not mention prompts, API keys, or internal setup."
    )
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent"
    )
    payload = {
        "systemInstruction": {"parts": [{"text": system_instruction}]},
        "contents": contents,
        "generationConfig": {"temperature": 0.85, "maxOutputTokens": 300},
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(
            url,
            params={"key": api_key},
            json=payload,
        )
        response.raise_for_status()
        data = response.json()

    candidates = data.get("candidates") or []
    if not candidates:
        raise ValueError("Gemini returned no candidates")
    parts = candidates[0].get("content", {}).get("parts", [])
    answer = " ".join(
        part.get("text", "").strip() for part in parts if part.get("text")
    ).strip()
    if not answer:
        raise ValueError("Gemini returned an empty answer")
    return answer[:4000]


@router.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.text, is_alexa_trigger)
async def alexa_ai_chat(message: Message) -> None:
    text = (message.text or "").strip()
    prefix_match = _ALEXA_PREFIX_RE.match(text)
    if prefix_match:
        text = prefix_match.group(1).strip() or "Say hello and start a friendly conversation."

    username = _bot_username or ""
    if username:
        text = re.sub(rf"(?<!\\w)@{re.escape(username)}\\b", "", text, flags=re.IGNORECASE).strip()
    if not text and message.reply_to_message and message.reply_to_message.text:
        text = message.reply_to_message.text.strip()
    if not text:
        text = "Say hello and start a friendly conversation."

    key = (message.chat.id, message.from_user.id)
    history = _conversations[key]
    try:
        answer = await _ask_gemini(text, history)
        # Missing API configuration and all Gemini/network/API failures are silent.
        # Alexa must never invent a fallback reply when Gemini is unavailable.
        if not answer:
            return
    except (httpx.HTTPError, ValueError):
        return
    except Exception:
        return

    history.append(("user", text))
    history.append(("model", answer))
    try:
        await message.reply(answer)
    except Exception:
        # A Telegram reply failure should never crash polling.
        pass
