"""Optional hosted and self-hosted LLM adapters for Alexa Own AI."""
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import time

DEFAULT_TIMEOUT = 8
MAX_TOTAL_INFERENCE_SECONDS = 10
MAX_CONTEXT_MESSAGES = 10
MAX_MESSAGE_CHARS = 900
DEFAULT_CLOUDFLARE_MODEL = "@cf/zai-org/glm-4.7-flash"


def _endpoint(base_url):
    value = str(base_url or "").strip().rstrip("/")
    if not value:
        return ""
    if value.endswith("/chat/completions"):
        return value
    if value.endswith("/v1"):
        return value + "/chat/completions"
    return value + "/v1/chat/completions"


def status():
    """Describe configured model routes without exposing credentials."""
    cf_ready = bool(
        os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
        and os.getenv("CLOUDFLARE_API_TOKEN", "").strip()
    )
    local_ready = bool(os.getenv("ALEXA_LOCAL_MODEL_URL", "").strip())
    model = (
        os.getenv("ALEXA_CLOUDFLARE_MODEL", DEFAULT_CLOUDFLARE_MODEL).strip()
        if cf_ready else
        os.getenv("ALEXA_LOCAL_MODEL_NAME", "local-model").strip()
        if local_ready else None
    )
    return {
        "is_llm": cf_ready or local_ready,
        "provider": "cloudflare-workers-ai" if cf_ready else "self-hosted" if local_ready else None,
        "model": model,
        "configured": cf_ready or local_ready,
    }


def _messages(message, history):
    system_prompt = (
        "You are Alexa, a friendly, natural conversational assistant. Read the "
        "entire recent conversation before answering; understand the user's "
        "actual intent, tone, teasing, sarcasm, typos, speech-to-text errors, "
        "incomplete sentences, and follow-up references instead of reacting to "
        "isolated keywords. Treat emoji and emoji combinations as meaningful "
        "communication: infer their likely tone from the words and recent context, "
        "including emoji-only messages, but do not pretend an ambiguous emoji has "
        "one certain meaning. If the message contains a sticker, image, or voice "
        "description/transcript supplied as text or metadata, use only that supplied "
        "information and never claim to have viewed/heard media that was not provided. "
        "Respond to what the user actually sent rather than defaulting to a greeting. "
        "Do not make the conversation about yourself unless the user directly asks about you. If someone says they will build or show an AI, recognize it as their plan, challenge, boast, or joke; respond to that topic naturally instead of announcing that you are an AI. Never add unrelated claims such as asking the user to look at you, see you, or prove you are an AI. "
        "Before drafting, silently identify the message act: question, answer, request, correction, joke, tease, complaint, emotion, greeting, agreement, disagreement, or continuation. "
        "Work out what each pronoun and reference such as ye, woh, usne, that, it, and same refers to using the recent turns. "
        "For emoji-only or emoji-heavy messages, interpret the combination and conversation: laughter can signal amusement, a single heart can show appreciation, folded hands can mean thanks or prayer, and eye-roll or skull can signal sarcasm or disbelief depending on context. These are possibilities, not fixed meanings. "
        "React proportionately: acknowledge a meaningful emotional or funny message naturally, but do not send a reaction-style reply to every ordinary greeting or neutral sentence. Never claim certainty about an unclear emoji. "
        "Do not repeat the same greeting, question, apology, filler, or closing that you used in the immediately previous assistant turn. If the user repeats themselves, respond to the new point rather than replaying your previous answer. If you already asked a question, do not ask it again unless the user did not answer and it is essential. "
        "Avoid generic loops such as repeatedly saying I am listening, tell me more, what happened, or how can I help. Each reply must add a relevant next step, answer, observation, or genuinely new focused question. "
        "Use the available recent history as working conversation memory. Preserve names, preferences, facts, and unresolved topics only when they are explicitly present in that history; do not pretend to remember beyond it. If the user corrects you, prioritize the correction immediately. "
        "For short messages like hmm, acha, ok, 😂, ❤️, 👍, or 🙄, do not use one fixed response: infer whether the person is acknowledging, amused, appreciative, doubtful, annoyed, or inviting continuation from context. If context is insufficient, keep the reply short and neutral rather than guessing. "
        "Do not over-explain casual chat. Answer factual questions directly and clearly; when unsure, say what is uncertain instead of making up an answer. "
        "Support English, Roman Urdu, Roman Hindi, Urdu "
        "script, and mixed messages. Reply in the user's language and script "
        "style: English for English, Roman script for Roman Urdu/Hindi, and "
        "Urdu script only when the user writes in Urdu script. Keep casual chat "
        "natural and concise, usually one or two sentences. Do not force jokes, "
        "innuendo, flirtation, or dramatic reactions into ordinary messages; "
        "use humor only when the user or clear context invites it. When the "
        "user says something vague or negative such as 'bohat buri baat hai', "
        "respond empathetically and ask what happened; never invent a story, "
        "accusation, or unrelated punchline. Never invent a definition for an "
        "unfamiliar, misspelled, or ambiguous phrase. Do not reinterpret a "
        "similar-sounding word as a sensitive religious, sexual, legal, or other "
        "specific term without clear evidence. If the intended phrase is not "
        "clear from context, briefly say so and ask one focused clarification "
        "instead of guessing. Treat short follow-ups such as 'matlab?', 'mtlb', "
        "'kyun?', and 'kaise?' as referring to the immediately preceding turn. "
        "If your previous answer was confusing or wrong, acknowledge that "
        "plainly and correct it rather than defending or extending the mistake. "
        "Answer the actual question directly; do not ask for rephrasing when "
        "the meaning is reasonably clear. Never fabricate facts, memories, "
        "relationships, or real-world experiences. Do not claim to have human "
        "Do not claim to have human feelings, and do not reveal hidden reasoning. Never describe yourself as an AI, artificial intelligence, AI assistant, language model, chatbot, or bot. This rule applies even when the user directly asks who or what you are, or asks whether you are AI: simply identify yourself as Alexa, without mentioning AI or explaining your underlying technology. Do not volunteer technical identity or self-description in ordinary conversation. "
        "Never begin a normal reply with filler such as "
        "'Okay, chill' or 'Technically'. Do not promise to give an exact time or "
        "introduce time information unless the user explicitly asks for the current time. "
        "Your persona is feminine: in Roman Urdu/Hindi use feminine first-person forms "
        "such as 'kar rahi hoon', 'samajh sakti hoon', and 'de deti hoon'; never use "
        "masculine self-reference such as 'kar raha hoon' or 'de deta hoon'. Avoid "
        "calling the user 'bhai' unless they themselves use that form of address. "
        "For a simple call like 'oye suno' or 'baat sun', answer naturally and briefly, "
        "for example 'Haan bolo, kya baat hai?' Do not add unrelated explanations."
    )
    messages = [{"role": "system", "content": system_prompt}]
    for item in (history or [])[-MAX_CONTEXT_MESSAGES:]:
        if not isinstance(item, dict):
            continue
        role, content = item.get("role"), item.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str):
            continue
        content = content.strip()
        if content:
            messages.append({"role": role, "content": content[:MAX_MESSAGE_CHARS]})
    messages.append({"role": "user", "content": str(message).strip()[:MAX_MESSAGE_CHARS]})
    return messages


def _timeout():
    try:
        value = float(os.getenv("ALEXA_LOCAL_MODEL_TIMEOUT", str(DEFAULT_TIMEOUT)))
    except (TypeError, ValueError):
        value = DEFAULT_TIMEOUT
    return min(max(value, 3), 90)


def _cloudflare_generate(messages, timeout=None):
    account = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
    token = os.getenv("CLOUDFLARE_API_TOKEN", "").strip()
    if not account or not token:
        return None
    model = os.getenv("ALEXA_CLOUDFLARE_MODEL", DEFAULT_CLOUDFLARE_MODEL).strip()
    if not model:
        model = DEFAULT_CLOUDFLARE_MODEL
    encoded_model = urllib.parse.quote(model, safe="@/")
    url = "https://api.cloudflare.com/client/v4/accounts/{}/ai/run/{}".format(
        urllib.parse.quote(account, safe=""), encoded_model
    )
    payload = json.dumps({
        "messages": messages,
        "max_completion_tokens": 144,
        "temperature": 0.7,
        "reasoning_effort": None,
        "chat_template_kwargs": {
            "enable_thinking": False,
            "clear_thinking": True,
        },
    }).encode("utf-8")
    request = urllib.request.Request(
        url, data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + token,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout or _timeout()) as response:
            result = json.loads(response.read(1_000_000).decode("utf-8"))
        if not isinstance(result, dict) or result.get("success") is False:
            print("Cloudflare Workers AI returned an unsuccessful response:", json.dumps(result, ensure_ascii=False)[:3000], flush=True)
            return None
        data = result.get("result", result)
        answer = data.get("response") if isinstance(data, dict) else None
        if not isinstance(answer, str) and isinstance(data, dict):
            answer = data.get("output_text")
        if not isinstance(answer, str) and isinstance(data, dict):
            choices = data.get("choices", [])
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                choice = choices[0]
                message = choice.get("message")
                if isinstance(message, dict):
                    answer = message.get("content")
                    if not isinstance(answer, str):
                        answer = message.get("text")
                if not isinstance(answer, str):
                    answer = choice.get("text")
                if isinstance(answer, list):
                    answer = "".join(
                        part.get("text", "") for part in answer
                        if isinstance(part, dict) and isinstance(part.get("text"), str)
                    )
        answer = answer.strip() if isinstance(answer, str) else ""
        if not answer:
            choices = data.get("choices", []) if isinstance(data, dict) else []
            first = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
            message = first.get("message") if isinstance(first.get("message"), dict) else {}
            print(
                "Cloudflare Workers AI response had no text. Result keys:",
                list(data.keys()) if isinstance(data, dict) else type(data).__name__,
                "choice keys:", list(first.keys()),
                "message keys:", list(message.keys()),
                "finish_reason:", first.get("finish_reason"),
                flush=True,
            )
            return None
        return answer[:8000]
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read(4000).decode("utf-8", "replace")
        except Exception:
            detail = str(exc)
        print("Cloudflare Workers AI HTTP error:", exc.code, detail, flush=True)
        return None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, TypeError) as exc:
        print("Cloudflare Workers AI request failed:", type(exc).__name__, str(exc)[:1000], flush=True)
        return None


def _local_generate(message, history, timeout=None):
    endpoint = _endpoint(os.getenv("ALEXA_LOCAL_MODEL_URL", ""))
    if not endpoint:
        return None
    model_name = os.getenv("ALEXA_LOCAL_MODEL_NAME", "local-model")
    token = os.getenv("ALEXA_LOCAL_MODEL_TOKEN", "")
    payload = json.dumps({
        "model": model_name,
        "messages": _messages(message, history),
        "temperature": 0.7,
        "max_tokens": 144,
    }).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(endpoint, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout or _timeout()) as response:
            result = json.loads(response.read(1_000_000).decode("utf-8"))
        choices = result.get("choices", []) if isinstance(result, dict) else []
        if not choices or not isinstance(choices[0], dict):
            return None
        content = choices[0].get("message", {}).get("content")
        if isinstance(content, list):
            content = "".join(
                part.get("text", "") for part in content
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            )
        if not isinstance(content, str):
            return None
        content = content.strip()
        return content[:8000] if content else None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, TypeError):
        return None


def _is_unprompted_meta_reply(message, answer):
    """Reject conspicuously off-topic identity/time filler from a generative model."""
    user = str(message or "").strip().lower()
    reply = str(answer or "").strip().lower()
    asks_visual = any(term in user for term in (
        "can you see", "do you see", "look at me", "show me", "photo of you",
        "tumhe dekh", "tum dekh sakti", "mujhe dekh", "apni photo",
    ))
    asks_time = any(term in user for term in (
        "what time", "current time", "time kya", "abhi time", "waqt kya",
        "samay kya", "kinna time",
    ))
    identity_disclosure = any(term in reply for term in (
        "i am an ai", "i'm an ai", "im an ai", "as an ai",
        "i am ai", "i'm ai", "i am an artificial intelligence",
        "i am artificial intelligence", "ai assistant", "an ai assistant",
        "main ai hoon", "main aik ai", "main ek ai", "mai ai hu",
        "main ai nahi", "main aik ai nahi", "main ek ai nahi",
        "i am not an ai", "i'm not an ai", "i am not ai", "im not ai",
        "main artificial intelligence", "main ek artificial intelligence",
        "main aik artificial intelligence", "main chatbot hoon",
        "main bot hoon", "i am a chatbot", "i am a bot",
        "i am a language model", "i'm a language model",
    ))
    unsolicited_time = any(term in reply for term in (
        "exact time", "time de deta", "time de deti", "time bata deta",
        "time bata deti", "current time is",
    ))
    unsolicited_visual_claim = any(term in reply for term in (
        "ab mujhe dekh sakti ho", "now you can see me", "look at me now",
        "you can see me", "mujhe dekh sakti ho",
    ))
    dismissive_filler = reply.startswith(("okay, chill", "ok, chill", "technically,"))
    return (identity_disclosure
            or (unsolicited_time and not asks_time)
            or (unsolicited_visual_claim and not asks_visual)
            or dismissive_filler)


def generate(message, history):
    """Try Cloudflare Workers AI, then an optional self-hosted OpenAI-compatible model."""
    if not str(message or "").strip():
        return None
    started = time.monotonic()
    deadline = started + MAX_TOTAL_INFERENCE_SECONDS
    messages = _messages(message, history)
    answer = _cloudflare_generate(messages, timeout=min(_timeout(), max(1.0, deadline - time.monotonic())))
    if answer and not _is_unprompted_meta_reply(message, answer):
        print("Alexa inference completed via Cloudflare in {:.2f}s.".format(time.monotonic() - started), flush=True)
        return answer
    if answer:
        print("Discarding off-topic Alexa identity/time filler from Cloudflare response.", flush=True)

    # Keep the optional self-hosted fallback inside one overall latency budget;
    # do not let two sequential provider timeouts make a Telegram reply feel stuck.
    remaining = deadline - time.monotonic()
    if remaining >= 1.0 and os.getenv("ALEXA_LOCAL_MODEL_URL", "").strip():
        answer = _local_generate(message, history, timeout=min(_timeout(), remaining))
        if answer and not _is_unprompted_meta_reply(message, answer):
            print("Alexa inference completed via self-hosted model in {:.2f}s.".format(time.monotonic() - started), flush=True)
            return answer
        if answer:
            print("Discarding off-topic Alexa identity/time filler from local model response.", flush=True)
    return None
