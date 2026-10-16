import asyncio
import structlog
from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject
from sqlalchemy import select
from typing import Any, Awaitable, Callable
from bot.database.models import GroupSettings

logger = structlog.get_logger()


class AutoDeleteCommandMiddleware(BaseMiddleware):
    """Delete group command messages after the configured per-group delay."""
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        result = await handler(event, data)
        if not isinstance(event, Message) or event.chat.type not in ("group", "supergroup"):
            return result
        raw = event.text or event.caption or ""
        if not raw.startswith("/"):
            return result
        session = data.get("db_session")
        if session is None:
            return result
        try:
            settings = await session.scalar(
                select(GroupSettings).where(GroupSettings.group_id == event.chat.id)
            )
            seconds = int(settings.auto_delete_seconds or 0) if settings else 0
        except Exception as exc:
            logger.debug("Auto-delete setting lookup failed", error=str(exc))
            return result
        if seconds > 0:
            async def delete_later():
                await asyncio.sleep(seconds)
                try:
                    await event.delete()
                except Exception as exc:
                    logger.debug("Could not auto-delete command message", chat_id=event.chat.id, error=str(exc))
            asyncio.create_task(delete_later())
        return result
