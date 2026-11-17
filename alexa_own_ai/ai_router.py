"""AI-first conversation routing while preserving Alexa's deterministic intents."""
import re

try:
    from .conversation_brain import respond as _conversation_respond
    from .model_runtime import generate as _model_generate
except ImportError:
    from conversation_brain import respond as _conversation_respond
    from model_runtime import generate as _model_generate


def route_response(message, normalized, language, history):
    """Use trusted local dialogue matches first, then the hosted LLM for open chat.

    Return None for local app.py handlers (math, date/time, and other exact
    intents) when no conversational-brain match or model reply is available.
    """
    local_reply = _conversation_respond(message, normalized, language, history)
    if local_reply:
        return local_reply

    text = str(normalized or "").strip().lower()
    raw = str(message or "").strip()

    # Keep exact, deterministic utility intents in app.py rather than spending
    # a model call or allowing a generative answer to replace their result.
    if re.search(r"\d\s*[+*/%\-]\s*\d", text) or re.search(r"\d\s*[×÷^]\s*\d", raw):
        return None
    if re.search(r"\b(time kya|abhi time|abhi kitne? baje|kitna time|what time|current time|time now|waqt kya|abhi waqt|samay kya|kinna time|today date|date today|aaj date|aaj ki date|date kya|what date|what is the date|today.s date|aaj ki tareekh|ajj di tareekh|tareekh kya|aaj ka din|which day is it)\b", text):
        return None

    # Open-ended conversation, factual questions, and contextual follow-ups
    # belong to the generative model. If it is unavailable, app.py retains its
    # existing lightweight fallback behavior.
    return _model_generate(raw, history)
