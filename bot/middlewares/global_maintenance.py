"""Global owner maintenance gate: silently ignore every group update."""
from aiogram import BaseMiddleware


def _update_has_group_chat(update) -> bool:
    """Return True when an incoming Telegram update belongs to a group."""
    candidates = []
    for field in (
        "message",
        "edited_message",
        "channel_post",
        "edited_channel_post",
        "business_message",
        "edited_business_message",
        "my_chat_member",
        "chat_member",
        "chat_join_request",
    ):
        value = getattr(update, field, None)
        if value is not None:
            candidates.append(value)

    callback = getattr(update, "callback_query", None)
    if callback is not None and getattr(callback, "message", None) is not None:
        candidates.append(callback.message)

    return any(
        getattr(getattr(item, "chat", None), "type", None) in {"group", "supergroup"}
        for item in candidates
    )


class GlobalMaintenanceMiddleware(BaseMiddleware):
    """Let owner private controls work, but stop all group-side bot activity."""

    async def __call__(self, handler, event, data):
        from bot.handlers.owner import get_maintenance_mode

        if get_maintenance_mode() and _update_has_group_chat(event):
            callback = getattr(event, "callback_query", None)
            if callback is not None:
                try:
                    await callback.answer()
                except Exception:
                    pass
            return None
        return await handler(event, data)
