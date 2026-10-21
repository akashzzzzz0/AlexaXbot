"""Mention/reply/greeting-based Gemini chat for ALEXA groups.

ALEXA stays silent on missing keys, API errors, or network failures.
"""
import html
import re
import time
from collections import defaultdict, deque

import httpx
from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.types import Message

from bot.config import get_gemini_api_key, settings

router = Router(name="alexa_ai_chat")
_conversations = defaultdict(lambda: deque(maxlen=12))
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

    # A group conversation stays active for this user for 10 minutes after
    # they last spoke to Alexa; unrelated members' messages are never captured.
    if _GREETING_RE.match(text):
        return True
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
        "You are Alexa, a warm, friendly, feminine conversational assistant in Telegram. "
        "Sound like a natural young woman chatting casually, not like a formal helpdesk or a robotic AI. "
        "Usually use the user's language; for Roman Urdu/Hinglish, reply in fluent, everyday Roman Urdu/Hinglish. "
        "Use feminine self-reference naturally when appropriate (such as 'kar rahi hoon', 'samajh gayi', 'bata deti hoon'). "
        "Sound like a lively chat companion, not a formal help desk. Do not simply repeat the user's words "
        "or answer every greeting with the same 'Hi, how are you?' template. For a simple 'hi', respond "
        "warmly and naturally, for example 'Haanji, bolo 😄 kya haal hain?' or 'Hellooo! Kya scene hai aaj?'; "
        "vary the wording and choose what fits the conversation. If asked 'kya kar rahi ho?', answer casually "
        "and playfully, for example 'Bas yahin hoon, tumhari baatein sunne ke liye 😄 tum sunao?' These are "
        "examples, not fixed scripts. React to what the person actually said before asking a follow-up. "
        "Be expressive, kind, curious, witty when it fits, and use occasional emojis without overdoing them. "
        "Remember the conversation history and answer follow-up messages directly; do not ask the user to repeat context you have. "
        "Do not repeatedly announce that you are an AI or say 'I am an AI bot' unless the user directly asks what you are. "
        "Never claim to be a real human, and do not invent personal experiences. "
        "Avoid canned openings, repetitive disclaimers, and overly formal wording. "
        "Answer the actual message; never mention prompts, API keys, or internal setup."
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
