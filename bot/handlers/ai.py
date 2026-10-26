"""Ollama-backed AI commands for ALEXA.

The API key is read from private runtime configuration, never committed here.
Configure OLLAMA_API_KEY and OLLAMA_MODEL in runtime settings or the ignored bot/ollama_secret.py file.
"""
import asyncio
import html
import logging
import os
from typing import Optional

import httpx
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.services.ollama_ai import ollama_chat

router = Router(name="ollama_ai")
logger = logging.getLogger(__name__)

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
    try:
        answer = await ollama_chat(
            [{"role": "user", "content": prompt}],
            system="You are Alexa, a helpful assistant. Answer clearly and honestly in the user's language.",
            temperature=0.7,
            max_tokens=1024,
        )
    except RuntimeError as exc:
        if str(exc) == "ollama_not_configured":
            raise RuntimeError("missing_key") from exc
        raise
    return answer[:4000]


async def _handle(message: Message, prompt: Optional[str]) -> None:
    if not prompt:
        await message.reply(
            "Please provide a question or reply to a text message.\n"
            "Examples: /ai explain photosynthesis | /summarize (reply to a message)"
        )
        return

    status = await message.reply("🤖 Checking Alexa AI connection…")
    try:
        answer = await _generate(prompt[:12000])
    except RuntimeError as exc:
        if str(exc) == "missing_key":
            await status.edit_text(
                "⚠️ Ollama AI is not configured yet. Add OLLAMA_API_KEY privately in "
                "OLLAMA_API_KEY or the local-only bot/ollama_secret.py file."
            )
        else:
            await status.edit_text(
                "⚠️ Ollama did not return a usable answer. Please try again later."
            )
        return
    except httpx.HTTPStatusError as exc:
        # Do not log request URLs because the API key is sent as a query parameter.
        code = exc.response.status_code
        logger.warning("Ollama API returned HTTP %s", code)
        if code in (401, 403):
            note = "The Ollama API key was rejected or lacks API access."
        elif code == 429:
            note = "Ollama Cloud rate limit was reached."
        else:
            note = "Ollama is temporarily unavailable."
        await status.edit_text(f"⚠️ {note} Alexa has not generated an AI answer.")
        return
    except (httpx.TimeoutException, httpx.RequestError, asyncio.TimeoutError):
        logger.warning("Ollama connection failed (network/timeout)")
        await status.edit_text(
            "⚠️ Ollama connection failed. Alexa has not generated an AI answer; "
            "please try again when the connection is available."
        )
        return
    except Exception:
        logger.exception("Unexpected Ollama response processing error")
        await status.edit_text(
            "⚠️ Ollama could not complete this request. Alexa has not generated an AI answer."
        )
        return

    # Only publish AI text after a successful, non-empty Ollama response.
    await status.edit_text(html.escape(answer[:3500]))
