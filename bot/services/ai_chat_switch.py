"""Process-wide runtime switch for Alexa's casual AI chat replies."""

_chat_enabled = True


def set_chat_enabled(enabled: bool) -> None:
    global _chat_enabled
    _chat_enabled = bool(enabled)


def is_chat_enabled() -> bool:
    return _chat_enabled
