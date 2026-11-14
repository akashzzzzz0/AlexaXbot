"""Sequential Alexa chat providers with safe response parsing and fallback."""
import asyncio
import os
import re

import httpx

from bot.config import get_gemini_api_key, settings




def _gemini_api_key() -> str:
    return get_gemini_api_key()


async def _gemini_chat(messages, *, system="", temperature=0.7, max_tokens=500, json_mode=False) -> str:
    api_key = _gemini_api_key()
    if not api_key:
        raise RuntimeError("gemini_not_configured")
    contents = []
    for item in messages:
        role = str(item.get("role", "user"))
        if role == "assistant":
            role = "model"
        if role not in {"user", "model"}:
            continue
        content = str(item.get("content", item.get("text", ""))).strip()
        if content:
            contents.append({"role": role, "parts": [{"text": content}]})
    if not contents and system.strip():
        contents.append({"role": "user", "parts": [{"text": system.strip()}]})
    if not contents:
        raise ValueError("empty_prompt")
    model = (getattr(settings, "GEMINI_MODEL", "") or os.getenv("GEMINI_MODEL", "") or "gemini-2.5-flash").strip()
    model = model.removeprefix("models/")
    payload = {"contents": contents, "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens}}
    if system.strip():
        payload["systemInstruction"] = {"parts": [{"text": system.strip()}]}
    if json_mode:
        payload["generationConfig"]["responseMimeType"] = "application/json"
    timeout = httpx.Timeout(14.0, connect=5.0)
    url = "https://generativelanguage.googleapis.com/v1beta/models/" + model + ":generateContent"
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, params={"key": api_key}, json=payload)
        if response.status_code >= 400:
            try:
                detail = response.json().get("error", {}).get("message", response.text[:300])
            except (ValueError, AttributeError):
                detail = response.text[:300]
            raise httpx.HTTPStatusError("Gemini HTTP " + str(response.status_code) + ": " + str(detail)[:220], request=response.request, response=response)
        data = response.json()
    candidates = data.get("candidates") or []
    parts = ((candidates[0].get("content") or {}).get("parts") or []) if candidates else []
    answer = "".join(str(part.get("text", "")) for part in parts if isinstance(part, dict)).strip()
    if not answer:
        raise RuntimeError("gemini_empty_response")
    return answer

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



def _xai_api_key() -> str:
    return (
        getattr(settings, "XAI_API_KEY", "")
        or os.getenv("XAI_API_KEY", "")
        or getattr(settings, "GROK_API_KEY", "")
        or os.getenv("GROK_API_KEY", "")
    ).strip()


async def _xai_chat(messages, *, system="", temperature=0.7, max_tokens=500, json_mode=False) -> str:
    api_key = _xai_api_key()
    if not api_key:
        raise RuntimeError("grok_not_configured")
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
    model = (getattr(settings, "GROK_MODEL", "") or os.getenv("GROK_MODEL", "") or "grok-3-mini").strip()
    payload = {"model": model, "messages": chat_messages, "temperature": temperature, "max_tokens": max_tokens}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    timeout = httpx.Timeout(12.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            "https://api.x.ai/v1/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        if response.status_code >= 400:
            try:
                detail = response.json().get("error", {}).get("message", response.text[:300])
            except (ValueError, AttributeError):
                detail = response.text[:300]
            raise httpx.HTTPStatusError(
                f"Grok chat HTTP {response.status_code}: {detail}",
                request=response.request,
                response=response,
            )
        data = response.json()
    choices = data.get("choices") or []
    answer = str((choices[0].get("message") or {}).get("content") or "").strip() if choices else ""
    if not answer:
        raise RuntimeError("grok_empty_response")
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
    """Try Gemini, Grok, then OpenAI; continue after errors or empty replies."""
    providers = [
        ("gemini", _gemini_chat, bool(_gemini_api_key()), 6.0),
        ("grok", _xai_chat, bool(_xai_api_key()), 6.0),
        ("openai", _openai_chat, bool(_openai_api_key()), 6.0),
    ]
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 20.0
    for name, provider, configured, timeout_seconds in providers:
        if not configured:
            print("Alexa chat provider skipped:", name + ":missing_runtime_configuration")
            continue
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        try:
            answer = await asyncio.wait_for(
                provider(
                    messages,
                    system=system,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    json_mode=json_mode,
                ),
                timeout=min(timeout_seconds, remaining),
            )
            if isinstance(answer, str) and answer.strip():
                return answer.strip()
            raise RuntimeError(name + "_empty_response")
        except Exception as exc:
            print("Alexa chat provider failed:", _safe_provider_error(name, exc))
    return ""
