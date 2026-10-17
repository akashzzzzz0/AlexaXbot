import asyncio
from aiogram import BaseMiddleware
from aiogram.types import Message, CallbackQuery
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


# Track private /start welcome menus so they disappear after 10 seconds only
# when the user has not pressed any inline option.
_start_menu_tasks = {}


def schedule_start_menu_delete(message: Message, seconds: int = 10):
    key = (message.chat.id, message.message_id)
    old_task = _start_menu_tasks.pop(key, None)
    if old_task:
        old_task.cancel()

    async def delete_if_unused():
        try:
            await asyncio.sleep(seconds)
            await message.delete()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            _start_menu_tasks.pop(key, None)

    _start_menu_tasks[key] = asyncio.create_task(delete_if_unused())


class StartMenuAutoDeleteCallbackMiddleware(BaseMiddleware):
    """Keep a start menu if any inline callback is pressed within its timeout."""
    async def __call__(self, handler, event, data):
        if isinstance(event, CallbackQuery) and event.message:
            key = (event.message.chat.id, event.message.message_id)
            task = _start_menu_tasks.pop(key, None)
            if task:
                task.cancel()
        return await handler(event, data)
