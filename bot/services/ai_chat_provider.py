"""Alexa chat provider chain: free Groq -> free Gemini only.

If both providers fail, return an empty response so Alexa stays silent. Paid OpenAI/Ollama providers are not used for chat.
Other Gemini/Ollama integrations remain independent.
"""
import asyncio
import os
import re

import httpx

from bot.config import get_gemini_api_key, settings




def _cloudflare_account_id() -> str:
    return (getattr(settings, "CLOUDFLARE_ACCOUNT_ID", "") or os.getenv("CLOUDFLARE_ACCOUNT_ID", "")).strip()


def _cloudflare_api_token() -> str:
    return (getattr(settings, "CLOUDFLARE_API_TOKEN", "") or os.getenv("CLOUDFLARE_API_TOKEN", "")).strip()


async def _cloudflare_chat(messages, *, system="", temperature=0.7, max_tokens=500, json_mode=False) -> str:
    account_id = _cloudflare_account_id()
    api_token = _cloudflare_api_token()
    if not account_id or not api_token:
        raise RuntimeError("cloudflare_not_configured")

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

    model = (getattr(settings, "CLOUDFLARE_AI_MODEL", "") or os.getenv("CLOUDFLARE_AI_MODEL", "") or "@cf/zai-org/glm-4.7-flash").strip()
    payload = {"messages": chat_messages, "temperature": temperature, "max_tokens": max_tokens}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    timeout = httpx.Timeout(14.0, connect=5.0)
    endpoint = "https://api.cloudflare.com/client/v4/accounts/" + account_id + "/ai/run/" + model
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(endpoint, json=payload, headers={"Authorization": "Bearer " + api_token, "Content-Type": "application/json"})
        if response.status_code >= 400:
            try:
                data = response.json()
                detail = data.get("errors") or data.get("error") or response.text[:300]
            except (ValueError, AttributeError):
                detail = response.text[:300]
            raise httpx.HTTPStatusError("Cloudflare Workers AI HTTP " + str(response.status_code) + ": " + str(detail)[:220], request=response.request, response=response)
        data = response.json()

    result = data.get("result") or {}
    choices = result.get("choices") or []
    choice_content = ((choices[0].get("message") or {}).get("content") if choices else "")
    answer = str(result.get("response") or choice_content or data.get("response") or "").strip()
    if not answer:
        raise RuntimeError("cloudflare_empty_response")
    return answer


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
    """Try Gemini, Grok, OpenAI, then Cloudflare; continue after errors or empty replies."""
    providers = [
        ("gemini", _gemini_chat, bool(_gemini_api_key()), 8.0),
        ("grok", _xai_chat, bool(_xai_api_key()), 8.0),
        ("openai", _openai_chat, bool(_openai_api_key()), 8.0),
        ("cloudflare", _cloudflare_chat, bool(_cloudflare_account_id() and _cloudflare_api_token()), 8.0),
    ]
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 34.0
    for name, provider, configured, timeout_seconds in providers:
        if not configured:
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
