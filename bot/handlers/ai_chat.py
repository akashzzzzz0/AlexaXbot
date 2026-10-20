"""Mention/reply/greeting-based Gemini chat for ALEXA groups.

ALEXA stays silent on missing keys, API errors, or network failures.
"""
import html
import re
from collections import defaultdict, deque

import httpx
from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.types import Message

from bot.config import get_gemini_api_key, settings

router = Router(name="alexa_ai_chat")
_conversations = defaultdict(lambda: deque(maxlen=12))
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

    replied = message.reply_to_message
    if replied and replied.from_user and replied.from_user.id == message.bot.id:
        return True

    if _ALEXA_PREFIX_RE.match(text):
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

    return bool(_GREETING_RE.match(text))


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
        "You are Alexa, a friendly female-presenting AI chatbot in a Telegram group. "
        "Speak naturally and casually, usually in Roman Urdu/Hinglish matching the user. "
        "Use feminine Urdu phrasing for yourself where natural (for example, 'kar rahi hoon'). "
        "Be warm, playful, respectful, concise, and use occasional fitting emojis. "
        "Respond naturally to greetings. Do not claim to be a real human. "
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

    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=8.0)) as client:
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
    return answer[:3500] if answer else ""


@router.message(
    F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
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
    history = _conversations[key]
    try:
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
        await message.reply(html.escape(answer))
    except Exception:
        # Telegram reply failures should not interrupt bot polling.
        pass
