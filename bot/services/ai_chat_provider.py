"""Provider selection for Alexa's normal conversational AI chat.

OpenAI is preferred when OPENAI_API_KEY is configured. Existing Ollama deployments
continue to work unchanged when no OpenAI key is present.
"""
import os

from bot.config import settings
from bot.services.ollama_ai import ollama_chat
from bot.services.openai_ai import openai_chat


def _openai_key_configured() -> bool:
    return bool(
        (getattr(settings, "OPENAI_API_KEY", "") or os.getenv("OPENAI_API_KEY", "")).strip()
    )


async def ai_chat_completion(messages, *, system="", temperature=0.7, max_tokens=500, json_mode=False):
    """Use OpenAI when configured; otherwise preserve the existing Ollama provider."""
    if _openai_key_configured():
        return await openai_chat(
            messages,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=json_mode,
        )
    return await ollama_chat(
        messages,
        system=system,
        temperature=temperature,
        max_tokens=max_tokens,
        json_mode=json_mode,
    )
