from typing import Any, Awaitable, Callable
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery


class AdminActionMiddleware(BaseMiddleware):
    """Require Telegram group admins for sensitive group-management actions."""
    def __init__(self, prefixes=(), commands=()):
        self.prefixes = tuple(prefixes)
        self.commands = {str(c).lower().lstrip("/") for c in commands}

    async def __call__(self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]], event: TelegramObject, data: dict[str, Any]) -> Any:
        actor = data.get("event_from_user")
        if not actor:
            return await handler(event, data)

        chat = None
        text = ""
        callback_data = ""
        if isinstance(event, Message):
            chat = event.chat
            text = event.text or ""
        elif isinstance(event, CallbackQuery):
            callback_data = event.data or ""
            msg = event.message
            chat = getattr(msg, "chat", None) if msg else None
        else:
            return await handler(event, data)

        protected = False
        if callback_data:
            protected = any(callback_data.startswith(prefix) for prefix in self.prefixes)
        elif text.startswith("/"):
            command = text.split(None, 1)[0].split("@", 1)[0].lstrip("/").lower()
            protected = command in self.commands
        if not protected:
            return await handler(event, data)

        # Telegram represents an anonymous group admin as sender_chat == the group.
        # Only Telegram admins can post as the group itself, so let the command handler
        # request an explicit confirmation instead of rejecting the hidden sender.
        if isinstance(event, Message) and chat is not None:
            sender_chat = getattr(event, "sender_chat", None)
            if chat.type in ("group", "supergroup") and sender_chat and sender_chat.id == chat.id:
                return await handler(event, data)

        if chat is None or chat.type not in ("group", "supergroup"):
            if isinstance(event, CallbackQuery):
                await event.answer("This action is available to group admins only.", show_alert=True)
            else:
                await event.answer("This action is available to group admins only.")
            return None

        try:
            member = await event.bot.get_chat_member(chat.id, actor.id)
            if member.status not in ("administrator", "creator"):
                if isinstance(event, CallbackQuery):
                    await event.answer("Only group admins can use this control.", show_alert=True)
                else:
                    await event.answer("Only group admins can use this command.")
                return None
        except Exception:
            if isinstance(event, CallbackQuery):
                await event.answer("Could not verify your admin status. Try again.", show_alert=True)
            else:
                await event.answer("Could not verify your admin status. Try again.")
            return None

        return await handler(event, data)
