"""Sequential Alexa chat providers with safe response parsing and fallback."""
import asyncio
import os
import re
import time
from datetime import timezone
from email.utils import parsedate_to_datetime

import httpx

from bot.config import get_gemini_api_key, settings


# Avoid hammering providers during quota/network outages. Each provider is
# retried automatically after its short cooldown; no persistent state is used.
_provider_retry_after: dict[str, float] = {}
_provider_rate_limited_until: dict[str, float] = {}
_provider_events: list[str] = []


def drain_provider_events() -> list[str]:
    events = list(_provider_events)
    _provider_events.clear()
    return events

_PROVIDER_429_COOLDOWN = 300.0
# HTTP 402 is a billing/credit condition, not a transient network error.
# Recheck periodically without repeatedly hitting an unpaid provider.
_PROVIDER_402_COOLDOWN = 3600.0
_PROVIDER_ERROR_COOLDOWN = 30.0


def _parse_retry_delay(value) -> float | None:
    """Parse Retry-After seconds/date or a reset duration such as 2m3.5s."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        pass
    try:
        retry_at = parsedate_to_datetime(raw)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(0.0, retry_at.timestamp() - time.time())
    except (TypeError, ValueError, OverflowError):
        pass
    match = re.fullmatch(
        r"\s*(?:(\d+(?:\.\d+)?)d)?\s*"
        r"(?:(\d+(?:\.\d+)?)h)?\s*"
        r"(?:(\d+(?:\.\d+)?)m)?\s*"
        r"(?:(\d+(?:\.\d+)?)s)?\s*",
        raw,
        re.IGNORECASE,
    )
    if not match or not any(part is not None for part in match.groups()):
        return None
    days, hours, minutes, seconds = (float(part or 0) for part in match.groups())
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def _is_groq_daily_token_quota_error(exc: Exception) -> bool:
    """Detect organization/model daily token quota errors (shared by API keys)."""
    response = getattr(exc, "response", None)
    details = str(getattr(response, "text", "") or "")
    try:
        payload = response.json()
        details += " " + str(payload)
    except (AttributeError, TypeError, ValueError):
        pass
    normalized = details.lower()
    return "tokens per day" in normalized or bool(re.search(r"\\btpd\\b", normalized))


def _mark_groq_keys_rate_limited(key_count: int, cooldown: float) -> None:
    """Apply an organization-level quota cooldown to every configured Groq key."""
    retry_at = time.monotonic() + cooldown
    for index in range(1, key_count + 1):
        name = f"groq_key_{index}"
        _provider_retry_after[name] = retry_at
        _provider_rate_limited_until[name] = retry_at


def _groq_daily_quota_cooldown(exc: Exception) -> float:
    """Prefer the provider reset header; otherwise avoid retrying a daily quota for 24h."""
    cooldown = _groq_rate_limit_cooldown(exc)
    if cooldown == _PROVIDER_429_COOLDOWN:
        return 86400.0
    return cooldown


def _groq_rate_limit_cooldown(exc: Exception) -> float:
    """Use Groq's actual reset headers; fall back safely when headers are absent."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", {}) or {}
    retry_after = _parse_retry_delay(headers.get("retry-after"))
    if retry_after is not None:
        return max(1.0, retry_after)

    exhausted_resets = []
    for remaining_header, reset_header in (
        ("x-ratelimit-remaining-requests", "x-ratelimit-reset-requests"),
        ("x-ratelimit-remaining-tokens", "x-ratelimit-reset-tokens"),
    ):
        if str(headers.get(remaining_header, "")).strip() == "0":
            delay = _parse_retry_delay(headers.get(reset_header))
            if delay is not None:
                exhausted_resets.append(delay)
    if exhausted_resets:
        return max(1.0, max(exhausted_resets))

    reset_delays = [
        delay for delay in (
            _parse_retry_delay(headers.get("x-ratelimit-reset-requests")),
            _parse_retry_delay(headers.get("x-ratelimit-reset-tokens")),
        ) if delay is not None
    ]
    if reset_delays:
        return max(1.0, max(reset_delays))
    return _PROVIDER_429_COOLDOWN


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

def _groq_api_keys() -> list[str]:
    """Return configured Groq keys in priority order, without duplicates."""
    candidates = (
        getattr(settings, "GROQ_API_KEY", ""),
        os.getenv("GROQ_API_KEY", ""),
        getattr(settings, "GROQ_API_KEY_1", ""),
        os.getenv("GROQ_API_KEY_1", ""),
        getattr(settings, "GROQ_API_KEY_2", ""),
        os.getenv("GROQ_API_KEY_2", ""),
        getattr(settings, "GROQ_API_KEY_3", ""),
        os.getenv("GROQ_API_KEY_3", ""),
    )
    keys = []
    seen = set()
    for candidate in candidates:
        key = str(candidate or "").strip()
        if key and key not in seen:
            seen.add(key)
            keys.append(key)
    return keys


async def _groq_chat(
    messages, *, system="", temperature=0.7, max_tokens=500, json_mode=False,
    api_key=None,
) -> str:
    api_key = str(api_key or "").strip()
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
    # asyncio.TimeoutError often has an empty string; keep logging safe.
    detail_lines = str(exc).splitlines()
    detail = (detail_lines[0] if detail_lines else type(exc).__name__)[:220]
    detail = re.sub(r"(?i)(api[_-]?key|authorization|bearer)" + chr(92) + "s*[:=]" + chr(92) + "s*[^ ,;]+", "[REDACTED]", detail)
    detail = re.sub(r"AIza[0-9A-Za-z_-]{20,}", "[REDACTED]", detail)
    detail = re.sub(r"(?i)https?://\S+", "[URL]", detail)
    if status is not None:
        return f"{name}:HTTP_{status}:{detail}"
    return f"{name}:{type(exc).__name__}:{detail}"


async def ai_chat_completion(messages, *, system="", temperature=0.7, max_tokens=500, json_mode=False):
    """Try configured chat providers in order with bounded timeouts and cooldowns.

    Only Gemini and Groq are used for Alexa's conversational route.
    """
    # Gemini stays first. If it is unavailable, try each configured Groq key
    # in order; a failed/limited key must not prevent the next key from being tried.
    groq_keys = _groq_api_keys()
    providers = [("gemini", _gemini_chat, bool(_gemini_api_key()), 6.0, None)]
    providers.extend(
        (
            f"groq_key_{index}",
            lambda *args, _key=key, **kwargs: _groq_chat(*args, api_key=_key, **kwargs),
            True,
            6.0,
            index,
        )
        for index, key in enumerate(groq_keys, start=1)
    )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 25.0
    now = time.monotonic()
    attempted = False
    groq_429_count = 0
    for name, provider, configured, timeout_seconds, groq_index in providers:
        if not configured:
            print("Alexa chat provider skipped:", name + ":missing_runtime_configuration")
            continue
        retry_at = _provider_retry_after.get(name, 0.0)
        if now < retry_at:
            print("Alexa chat provider skipped:", name + ":temporary_cooldown")
            if groq_index is not None and _provider_rate_limited_until.get(name, 0.0) > now:
                groq_429_count += 1
            continue
        _provider_retry_after.pop(name, None)
        _provider_rate_limited_until.pop(name, None)
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        attempted = True
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
                _provider_retry_after.pop(name, None)
                _provider_rate_limited_until.pop(name, None)
                return answer.strip()
            raise RuntimeError(name + "_empty_response")
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status == 429:
                if groq_index is not None:
                    if _is_groq_daily_token_quota_error(exc):
                        cooldown = _groq_daily_quota_cooldown(exc)
                        # Groq daily token limits are shared at organization/model
                        # level, so rotating API keys cannot bypass this quota.
                        _mark_groq_keys_rate_limited(len(groq_keys), cooldown)
                        groq_429_count = len(groq_keys)
                        print(
                            "Alexa Groq organization daily token quota exhausted; "
                            "all configured keys cooling down:",
                            str(round(cooldown, 1)) + "s",
                        )
                    else:
                        cooldown = _groq_rate_limit_cooldown(exc)
                        groq_429_count += 1
                        _provider_rate_limited_until[name] = time.monotonic() + cooldown
                else:
                    cooldown = _PROVIDER_429_COOLDOWN
                    _provider_events.append(name)
            elif status == 402:
                cooldown = _PROVIDER_402_COOLDOWN
                _provider_rate_limited_until.pop(name, None)
            else:
                cooldown = _PROVIDER_ERROR_COOLDOWN
                _provider_rate_limited_until.pop(name, None)
            _provider_retry_after[name] = time.monotonic() + cooldown
            if status == 429 and groq_index is not None:
                print("Alexa Groq rate limit cooldown:", name, str(round(cooldown, 1)) + "s")
            print("Alexa chat provider failed:", _safe_provider_error(name, exc))
    if groq_keys and groq_429_count >= len(groq_keys):
        # Notify the owner only when every configured Groq key is rate-limited.
        if "groq" not in _provider_events:
            _provider_events.append("groq")
    if attempted:
        print("Alexa chat providers exhausted: no usable reply from Gemini or Groq keys")
    else:
        print("Alexa chat providers unavailable: all configured providers are in cooldown or missing")
    return ""
