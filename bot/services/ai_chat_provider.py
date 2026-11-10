"""Normal Alexa chat provider with a narrow Ollama-billing fallback.

Ollama remains the primary provider. Only an HTTP 402 response triggers the
OpenAI fallback, so existing Ollama behavior and all chat handlers stay intact.
API keys are read from private runtime settings/environment and never logged.
"""
import os

import httpx

from bot.config import settings
from bot.services.ollama_ai import ollama_chat


def _openai_api_key() -> str:
    return (
        getattr(settings, "OPENAI_API_KEY", "")
        or os.getenv("OPENAI_API_KEY", "")
    ).strip()


async def _openai_chat(
    messages, *, system="", temperature=0.7, max_tokens=500, json_mode=False
) -> str:
    api_key = _openai_api_key()
    if not api_key:
        raise RuntimeError("openai_not_configured")

    chat_messages = []
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
        "model": (getattr(settings, "OPENAI_MODEL", "") or os.getenv("OPENAI_MODEL", "") or "gpt-4.1-mini").strip(),
        "messages": chat_messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    timeout = httpx.Timeout(60.0, connect=10.0)
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
                f"OpenAI chat HTTP {response.status_code}: {detail}",
                request=response.request,
                response=response,
            )
        data = response.json()

    choices = data.get("choices") or []
    answer = str((choices[0].get("message") or {}).get("content") or "").strip() if choices else ""
    if not answer:
        raise RuntimeError("empty_response")
    return answer


async def ai_chat_completion(messages, *, system="", temperature=0.7, max_tokens=500, json_mode=False):
    """Use Ollama normally; fall back to OpenAI only when Ollama returns HTTP 402."""
    try:
        return await ollama_chat(
            messages,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=json_mode,
        )
    except httpx.HTTPStatusError as exc:
        if exc.response is None or exc.response.status_code != 402:
            raise
        if not _openai_api_key():
            raise RuntimeError("ollama_payment_required_and_openai_not_configured") from exc
        return await _openai_chat(
            messages,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=json_mode,
        )
