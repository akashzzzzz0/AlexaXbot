import json
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery
import structlog

logger = structlog.get_logger()

LOCALES_DIR = Path(__file__).parent.parent / "locales"
_translations: Dict[str, Dict[str, str]] = {}


def load_translations() -> None:
    for lang_file in LOCALES_DIR.glob("*.json"):
        lang_code = lang_file.stem
        with open(lang_file, "r", encoding="utf-8") as f:
            _translations[lang_code] = json.load(f)
    logger.info("Translations loaded", languages=list(_translations.keys()))


def get_text(lang: str, key: str, **kwargs) -> str:
    """Return English UI text. ALEXA is configured as an English-only bot."""
    en_dict = _translations.get("en") or {}
    text = en_dict.get(key) or key
    if kwargs:
        try:
            text = text.format(**kwargs)
        except (KeyError, ValueError):
            pass
    return text


class I18nMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        # Ignore stored and Telegram locale preferences: all bot responses stay English.
        lang = "en"

        def _(key: str, **kwargs) -> str:
            return get_text(lang, key, **kwargs)

        data["_"] = _
        data["lang"] = lang
        return await handler(event, data)
