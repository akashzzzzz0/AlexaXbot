"""Small async OpenAI Chat Completions client for Alexa's conversational chat.

Uses the project's existing httpx dependency; API keys are read only from runtime
configuration/environment and are never written to source or logs.
"""
import os
from typing import Iterable

import httpx

from bot.config import settings


def get_openai_api_key() -> str:
    return (
        getattr(settings, "OPENAI_API_KEY", "")
        or os.getenv("OPENAI_API_KEY", "")
    ).strip()


async def openai_chat(
    messages: Iterable[dict[str, str]],
    *,
    system: str = "",
    temperature: float = 0.7,
    max_tokens: int = 500,
    json_mode: bool = False,
) -> str:
    """Generate a non-streaming response while preserving supplied chat context."""
    api_key = get_openai_api_key()
    if not api_key:
        raise RuntimeError("openai_not_configured")

    model = (
        getattr(settings, "OPENAI_MODEL", "")
        or os.getenv("OPENAI_MODEL", "")
        or "gpt-4.1-mini"
    ).strip()
    chat_messages: list[dict[str, str]] = []
    if system.strip():
        chat_messages.append({"role": "system", "content": system.strip()})

    for item in messages:
        role = str(item.get("role", "user"))
        if role == "model":
            role = "assistant"
        if role not in {"system", "user", "assistant"}:
            continue
        content = str(item.get("content", item.get("text", ""))).strip()
        if content:
            chat_messages.append({"role": role, "content": content})

    if not chat_messages:
        raise ValueError("empty_prompt")

    payload = {
        "model": model,
        "messages": chat_messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    timeout = httpx.Timeout(45.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            "https://api.openai.com/v1/chat/completions",
            json=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        if response.status_code >= 400:
            try:
                detail = response.json().get("error", {}).get("message", response.text[:300])
            except (ValueError, AttributeError):
                detail = response.text[:300]
            raise httpx.HTTPStatusError(
                f"OpenAI HTTP {response.status_code}: {str(detail)[:300]}",
                request=response.request,
                response=response,
            )
        data = response.json()

    choices = data.get("choices") or []
    answer = str(((choices[0].get("message") or {}).get("content")) or "").strip() if choices else ""
    if not answer:
        raise RuntimeError("empty_response")
    return answer
