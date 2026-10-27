"""Shared Ollama Cloud/local chat client for ALEXA's AI features.

The bot keeps its existing handlers and UI; only the model-provider layer is shared here.
Configure OLLAMA_BASE_URL, OLLAMA_MODEL, and OLLAMA_API_KEY at runtime, or use the
ignored bot/ollama_secret.py file for a private key on hosts without environment controls.
"""
import os
from typing import Iterable

import httpx

from bot.config import settings


def get_ollama_api_key() -> str:
    key = (getattr(settings, "OLLAMA_API_KEY", "") or os.getenv("OLLAMA_API_KEY", "")).strip()
    if key:
        return key
    try:
        from bot.ollama_secret import OLLAMA_API_KEY
        return str(OLLAMA_API_KEY or "").strip()
    except (ImportError, AttributeError, TypeError):
        return ""


async def ollama_chat(
    messages: Iterable[dict[str, str]],
    *,
    system: str = "",
    temperature: float = 0.7,
    max_tokens: int = 500,
    json_mode: bool = False,
) -> str:
    """Generate one non-streaming answer from an Ollama-compatible /api/chat endpoint."""
    api_key = get_ollama_api_key()
    if not api_key:
        raise RuntimeError("ollama_not_configured")
    base_url = (getattr(settings, "OLLAMA_BASE_URL", "") or os.getenv("OLLAMA_BASE_URL", "") or "https://ollama.com").strip().rstrip("/")
    # Ollama Cloud's direct API expects the model's registry name without the
    # local CLI ":cloud" suffix. Keep runtime overrides supported.
    model = (getattr(settings, "OLLAMA_MODEL", "") or os.getenv("OLLAMA_MODEL", "") or "gemma4:31b").strip()
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
        "stream": False,
        "options": {"temperature": temperature, "num_predict": max_tokens},
    }
    if json_mode:
        payload["format"] = "json"
    timeout = httpx.Timeout(60.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            f"{base_url}/api/chat",
            json=payload,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        if response.status_code >= 400:
            # Preserve the provider's concise diagnostic in logs (never its URL/key).
            try:
                detail = response.json().get("error", response.text[:300])
            except ValueError:
                detail = response.text[:300]
            raise httpx.HTTPStatusError(
                f"Ollama HTTP {response.status_code}: {detail}",
                request=response.request,
                response=response,
            )
        data = response.json()
    message = data.get("message") or {}
    answer = str(message.get("content") or "").strip()
    if not answer:
        raise RuntimeError("empty_response")
    return answer
