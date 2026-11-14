"""Alexa chat provider chain: Groq -> Gemini -> Ollama -> OpenAI.

Provider keys are read only from private runtime settings/environment. The
existing chat handlers, prompts, history, and reply formatting remain unchanged.
"""
import asyncio
import os
import re

import httpx

from bot.config import get_gemini_api_key, settings
from bot.services.ollama_ai import ollama_chat




def _groq_api_key() -> str:
    return (
        getattr(settings, "GROQ_API_KEY", "")
        or os.getenv("GROQ_API_KEY", "")
    ).strip()


async def _groq_chat(
    messages, *, system="", temperature=0.7, max_tokens=500, json_mode=False
) -> str:
    api_key = _groq_api_key()
    if not api_key:
        raise RuntimeError("groq_not_configured")

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

    model = (
        getattr(settings, "GROQ_MODEL", "")
        or os.getenv("GROQ_MODEL", "")
        or "openai/gpt-oss-20b"
    ).strip()
    payload = {
        "model": model,
        "messages": chat_messages,
        "temperature": temperature,
        "max_completion_tokens": max_tokens,
        "reasoning_effort": "low",
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    timeout = httpx.Timeout(14.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
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
                f"Groq chat HTTP {response.status_code}: {detail}",
                request=response.request,
                response=response,
            )
        data = response.json()

    choices = data.get("choices") or []
    answer = str((choices[0].get("message") or {}).get("content") or "").strip() if choices else ""
    if not answer:
        raise RuntimeError("groq_empty_response")
    return answer


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
        raise RuntimeError("openai_empty_response")
    return answer


async def _gemini_chat(
    messages, *, system="", temperature=0.7, max_tokens=500, json_mode=False
) -> str:
    api_key = get_gemini_api_key()
    if not api_key:
        raise RuntimeError("gemini_not_configured")

    contents = []
    system_parts = [system.strip()] if system.strip() else []
    for item in messages:
        role = str(item.get("role", "user"))
        if role == "assistant":
            role = "model"
        if role == "system":
            content = str(item.get("content", item.get("text", ""))).strip()
            if content:
                system_parts.append(content)
            continue
        if role not in {"user", "model"}:
            continue
        content = str(item.get("content", item.get("text", ""))).strip()
        if content:
            contents.append({"role": role, "parts": [{"text": content}]})
    if not contents:
        raise ValueError("empty_prompt")

    model = (
        getattr(settings, "GEMINI_MODEL", "")
        or os.getenv("GEMINI_MODEL", "")
        or "gemini-3.5-flash-lite"
    ).strip()
    payload = {
        "contents": contents,
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
        },
    }
    if system_parts:
        payload["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_parts)}]}
    if json_mode:
        payload["generationConfig"]["responseMimeType"] = "application/json"

    timeout = httpx.Timeout(60.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            params={"key": api_key},
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        if response.status_code >= 400:
            try:
                detail = response.json().get("error", {}).get("message", response.text[:300])
            except (ValueError, AttributeError):
                detail = response.text[:300]
            raise httpx.HTTPStatusError(
                f"Gemini HTTP {response.status_code}: {detail}",
                request=response.request,
                response=response,
            )
        data = response.json()

    candidates = data.get("candidates") or []
    parts = ((candidates[0].get("content") or {}).get("parts") or []) if candidates else []
    answer = "".join(str(part.get("text") or "") for part in parts).strip()
    if not answer:
        raise RuntimeError("gemini_empty_response")
    return answer


def _safe_provider_error(name: str, exc: Exception) -> str:
    """Keep HTTP diagnostics useful without logging credentials or request URLs."""
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    detail = str(exc).splitlines()[0][:220]
    detail = re.sub(r"(?i)(api[_-]?key|authorization|bearer)" + chr(92) + "s*[:=]" + chr(92) + "s*[^ ,;]+", "[REDACTED]", detail)
    detail = re.sub(r"AIza[0-9A-Za-z_-]{20,}", "[REDACTED]", detail)
    detail = re.sub(r"(?i)https?://\S+", "[URL]", detail)
    if status is not None:
        return f"{name}:HTTP_{status}:{detail}"
    return f"{name}:{type(exc).__name__}:{detail}"


async def ai_chat_completion(messages, *, system="", temperature=0.7, max_tokens=500, json_mode=False):
    """Use Groq as the primary chat provider, then fall back safely."""
    provider_errors = []

    # Groq is preferred for Alexa chat; other configured providers remain
    # available as fallbacks if Groq is unavailable or rate-limited.
    providers = [
        ("groq", _groq_chat, bool(_groq_api_key()), 8.0),
        ("gemini", _gemini_chat, bool(get_gemini_api_key()), 12.0),
        ("ollama", ollama_chat, True, 28.0),
        ("openai", _openai_chat, bool(_openai_api_key()), 8.0),
    ]
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 52.0
    for name, provider, configured, timeout_seconds in providers:
        if not configured:
            provider_errors.append(f"{name}:not_configured")
            continue
        remaining = deadline - loop.time()
        if remaining <= 0:
            provider_errors.append(f"{name}:overall_timeout")
            break
        try:
            return await asyncio.wait_for(
                provider(
                    messages,
                    system=system,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    json_mode=json_mode,
                ),
                timeout=min(timeout_seconds, remaining),
            )
        except asyncio.TimeoutError:
            provider_errors.append(f"{name}:timeout")
        except Exception as exc:
            provider_errors.append(_safe_provider_error(name, exc))

    raise RuntimeError("all_ai_providers_failed:" + ",".join(provider_errors))
