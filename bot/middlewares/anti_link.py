from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject
from sqlalchemy import select

from bot.database.models import SecuritySettings
from bot.services.security_service import check_message_violations
import structlog

logger = structlog.get_logger()


class AntiLinkEnforcementMiddleware(BaseMiddleware):
    """Delete link-bearing group messages when that group's Anti-Link is enabled."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message) or event.chat.type not in ("group", "supergroup"):
            return await handler(event, data)

        sender = event.from_user
        if sender is None:
            return await handler(event, data)

        session = data.get("db_session")
        if session is None:
            return await handler(event, data)

        try:
            security = await session.scalar(
                select(SecuritySettings).where(SecuritySettings.group_id == event.chat.id)
            )
            if security is None or not (security.anti_link or security.anti_forward_spam):
                return await handler(event, data)

            member = await event.bot.get_chat_member(event.chat.id, sender.id)
            status = getattr(member.status, "value", member.status)
            if status in ("administrator", "creator"):
                return await handler(event, data)

            violations = await check_message_violations(event, security)
            matched = []
            if security.anti_link and "link" in violations:
                matched.append("link")
            if security.anti_forward_spam and "forward_spam" in violations:
                matched.append("forward")

            if not matched:
                return await handler(event, data)

            await event.delete()
            logger.info(
                "Group security deleted a message",
                group_id=event.chat.id,
                user_id=sender.id,
                message_id=event.message_id,
                protections=matched,
            )
            return None
        except Exception as exc:
            logger.warning(
                "Anti-Link enforcement failed",
                group_id=event.chat.id,
                message_id=event.message_id,
                error=str(exc),
            )
            return await handler(event, data)
