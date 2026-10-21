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
        "If the user corrects you or says your reply sounds robotic, acknowledge it casually and change your approach immediately instead of defending yourself." 
    )
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent"
    )
    payload = {
        "systemInstruction": {"parts": [{"text": system_instruction}]},
        "contents": contents,
        "generationConfig": {"temperature": 0.92, "topP": 0.94, "maxOutputTokens": 180},
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
    return answer[:700].rsplit(" ", 1)[0] if answer else ""


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

