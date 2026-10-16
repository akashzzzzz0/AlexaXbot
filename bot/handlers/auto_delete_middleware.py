import asyncio
from aiogram import BaseMiddleware
from aiogram.types import Message
from sqlalchemy import select
from bot.database.models import GroupSettings


class AutoDeleteCommandMiddleware(BaseMiddleware):
    """Delete group command messages after the configured per-group delay."""
    async def __call__(self, handler, event, data):
        if isinstance(event, Message) and event.chat.type in ("group", "supergroup"):
            text = event.text or event.caption or ""
            if text.startswith("/"):
                session = data.get("db_session")
                if session is not None:
                    try:
                        settings_row = await session.scalar(
                            select(GroupSettings).where(GroupSettings.group_id == event.chat.id)
                        )
                        delay = int(settings_row.auto_delete_seconds or 0) if settings_row else 0
                        if delay > 0:
                            async def delete_later(bot, chat_id, message_id, seconds):
                                await asyncio.sleep(seconds)
                                try:
                                    await bot.delete_message(chat_id=chat_id, message_id=message_id)
                                except Exception:
                                    pass
                            asyncio.create_task(delete_later(event.bot, event.chat.id, event.message_id, delay))
                    except Exception:
                        # Cleanup must never prevent a command from running.
                        pass
        return await handler(event, data)
