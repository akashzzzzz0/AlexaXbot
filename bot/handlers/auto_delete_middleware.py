import asyncio
from aiogram import BaseMiddleware
from aiogram.types import Message, CallbackQuery


# Pending idle timers for private /start menus, keyed by (chat_id, message_id).
_start_menu_tasks = {}


def cancel_start_menu_delete_for_chat(chat_id: int):
    for key, task in list(_start_menu_tasks.items()):
        if key[0] == chat_id:
            _start_menu_tasks.pop(key, None)
            task.cancel()


def schedule_start_menu_delete(message: Message, seconds: int = 10):
    key = (message.chat.id, message.message_id)
    old_task = _start_menu_tasks.pop(key, None)
    if old_task:
        old_task.cancel()

    async def delete_if_unused():
        try:
            await asyncio.sleep(seconds)
            # Delete only if no message or inline-button interaction used the bot.
            if _start_menu_tasks.get(key) is asyncio.current_task():
                await message.delete()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            if _start_menu_tasks.get(key) is asyncio.current_task():
                _start_menu_tasks.pop(key, None)

    _start_menu_tasks[key] = asyncio.create_task(delete_if_unused())


class StartMenuAutoDeleteMessageMiddleware(BaseMiddleware):
    """Any new private message means the user is actively using the bot."""
    async def __call__(self, handler, event, data):
        if isinstance(event, Message) and event.chat.type == "private":
            cancel_start_menu_delete_for_chat(event.chat.id)
        return await handler(event, data)


class StartMenuAutoDeleteCallbackMiddleware(BaseMiddleware):
    """Any inline-button interaction means the user is actively using the bot."""
    async def __call__(self, handler, event, data):
        if isinstance(event, CallbackQuery) and event.message:
            cancel_start_menu_delete_for_chat(event.message.chat.id)
        return await handler(event, data)
