"""Gemini AI commands for ALEXA.

The API key is read from private runtime configuration, never committed here.
Supports GEMINI_API_KEY/API_KEY environment settings or ignored bot/gemini_secret.py.
"""
import asyncio
import logging
import os
from typing import Optional

import httpx
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.config import settings

router = Router(name="gemini_ai")
logger = logging.getLogger(__name__)

MODEL = "gemini-2.5-flash"
API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"


def _api_key() -> str:
    key = (getattr(settings, "GEMINI_API_KEY", "") or
           os.getenv("GEMINI_API_KEY", "") or os.getenv("API_KEY", "")).strip()
    if key:
        return key
    # Optional local-only key file; .gitignore prevents it entering Git.
    try:
        from bot.gemini_secret import GEMINI_API_KEY
        return str(GEMINI_API_KEY).strip()
    except (ImportError, AttributeError, TypeError):
        return ""


def _message_text(message: Message) -> str:
    text = (message.text or message.caption or "").strip()
    replied = message.reply_to_message
    if text:
        # Remove the command and its arguments from the request body.
        parts = text.split(maxsplit=1)
        if parts and parts[0].startswith("/"):
            if len(parts) > 1 and parts[1].strip():
                return parts[1].strip()
            # Commands without arguments can operate on the replied-to message.
            if replied:
                return (replied.text or replied.caption or "").strip()
            return ""
        return text
    if replied:
        return (replied.text or replied.caption or "").strip()
    return ""


async def _generate(prompt: str) -> str:
    key = _api_key()
    if not key:
        raise RuntimeError("missing_key")

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 1024},
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(25.0, connect=8.0)) as client:
        response = await client.post(
            API_URL,
            params={"key": key},
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()
        data = response.json()

    candidates = data.get("candidates") or []
    if not candidates:
        raise RuntimeError("empty_response")
    content = candidates[0].get("content") or {}
    pieces = [
        part.get("text", "")
        for part in content.get("parts", [])
        if isinstance(part, dict) and part.get("text")
    ]
    answer = "\n".join(pieces).strip()
    if not answer:
        raise RuntimeError("empty_response")
    return answer[:4000]


async def _handle(message: Message, prompt: Optional[str]) -> None:
    if not prompt:
        await message.reply(
            "Please provide a question or reply to a text message.\n"
            "Examples: /ai explain photosynthesis | /summarize (reply to a message)"
        )
        return

    status = await message.reply("🤖 Checking Gemini connection…")
    try:
        answer = await _generate(prompt[:12000])
    except RuntimeError as exc:
        if str(exc) == "missing_key":
            await status.edit_text(
                "⚠️ Gemini is not configured yet. Add your key privately in "
                "GEMINI_API_KEY or the local-only bot/gemini_secret.py file."
            )
        else:
            await status.edit_text(
                "⚠️ Gemini did not return a usable answer. Please try again later."
            )
        return
    except httpx.HTTPStatusError as exc:
        # Do not log request URLs because the API key is sent as a query parameter.
        code = exc.response.status_code
        logger.warning("Gemini API returned HTTP %s", code)
        if code in (401, 403):
            note = "The Gemini key was rejected or lacks API access."
        elif code == 429:
            note = "Gemini rate limit or quota was reached."
        else:
            note = "Gemini is temporarily unavailable."
        await status.edit_text(f"⚠️ {note} Alexa has not generated an AI answer.")
        return
    except (httpx.TimeoutException, httpx.RequestError, asyncio.TimeoutError):
        logger.warning("Gemini connection failed (network/timeout)")
        await status.edit_text(
            "⚠️ Gemini connection failed. Alexa has not generated an AI answer; "
            "please try again when the connection is available."
        )
        return
    except Exception:
        logger.exception("Unexpected Gemini response processing error")
        await status.edit_text(
            "⚠️ Gemini could not complete this request. Alexa has not generated an AI answer."
        )
        return

    # Only publish AI text after a successful, non-empty Gemini response.
    await status.edit_text(answer)


@router.message(Command("ai"))
async def ai_command(message: Message) -> None:
    await _handle(message, _message_text(message))


@router.message(Command("rewrite"))
async def rewrite_command(message: Message) -> None:
    text = _message_text(message)
    if text:
        text = "Rewrite the following text clearly while preserving its meaning:\n\n" + text
    await _handle(message, text)


@router.message(Command("summarize"))
async def summarize_command(message: Message) -> None:
    text = _message_text(message)
    if text:
        text = "Summarize the following text accurately and concisely:\n\n" + text
    await _handle(message, text)


@router.message(Command("translate"))
async def translate_command(message: Message) -> None:
    text = _message_text(message)
    if text:
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            language = parts[0]
            replied = message.reply_to_message
            body = ((replied.text or replied.caption or "").strip() if replied else "")
            if not body:
                await message.reply("Usage: /translate <language> <text>, or reply to a message with /translate <language>.")
                return
        else:
            language, body = parts[0], parts[1]
        text = f"Translate the following text into {language}. Return only the translation:\n\n{body}"
    await _handle(message, text)
