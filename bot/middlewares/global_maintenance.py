"""Global owner maintenance gate: pause all non-owner bot interactions."""

from aiogram import BaseMiddleware


_ALLOWED_OWNER_CALLBACKS = frozenset(
    {
        "owner:menu",
        "owner:settings",
        "owner:health",
        "owner:stats",
        "owner:maintenance",
        "owner:maintenance:on",
        "owner:maintenance:off",
    }
)


def _update_is_owner_maintenance_control(update, owner_id: int | None) -> bool:
    """Allow only the owner controls needed to inspect or disable maintenance."""
    if not owner_id:
        return False

    callback = getattr(update, "callback_query", None)
    if callback is not None:
        message = getattr(callback, "message", None)
        chat = getattr(message, "chat", None)
        actor = getattr(callback, "from_user", None)
        return bool(
            getattr(chat, "type", None) == "private"
            and getattr(actor, "id", None) == owner_id
            and getattr(callback, "data", None) in _ALLOWED_OWNER_CALLBACKS
        )

    for field in ("message", "edited_message"):
        message = getattr(update, field, None)
        if message is None:
            continue
        chat = getattr(message, "chat", None)
        actor = getattr(message, "from_user", None)
        if (
            getattr(chat, "type", None) == "private"
            and getattr(actor, "id", None) == owner_id
        ):
            text = str(getattr(message, "text", "") or "").strip()
            if text:
                command = text.split(maxsplit=1)[0].split("@", 1)[0].casefold()
                if command == "/owner":
                    return True
    return False


class GlobalMaintenanceMiddleware(BaseMiddleware):
    """Pause all user-facing updates while preserving the owner's maintenance controls."""

    async def __call__(self, handler, event, data):
        from bot.config import settings
        from bot.handlers.owner import get_maintenance_mode

        if get_maintenance_mode() and not _update_is_owner_maintenance_control(
            event, settings.effective_owner_id
        ):
            callback = getattr(event, "callback_query", None)
            if callback is not None:
                try:
                    await callback.answer(
                        "ALEXA is under maintenance. Only owner maintenance controls are available.",
                        show_alert=True,
                    )
                except Exception:
                    pass
            return None
        return await handler(event, data)
