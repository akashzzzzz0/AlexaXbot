from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject
from sqlalchemy import select

from bot.database.models import SecuritySettings
from bot.services.security_service import check_message_violations
import structlog

logger = structlog.get_logger()


class AntiLinkEnforcementMiddleware(BaseMiddleware):
    """Enforce Anti-Link and Anti-Forward on new and edited group messages."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message) or event.chat.type not in ("group", "supergroup"):
            return await handler(event, data)

        # Do not moderate ALEXA's own messages. Channel posts and anonymous
        # senders are intentionally not discarded just because from_user is absent.
        sender = getattr(event, "from_user", None)
        if sender is not None and getattr(sender, "is_bot", False):
            return await handler(event, data)

        # An anonymous administrator posts as the group itself. Telegram only
        # permits this identity for group administrators.
        sender_chat = getattr(event, "sender_chat", None)
        if sender_chat is not None and sender_chat.id == event.chat.id:
            return await handler(event, data)

        session = data.get("db_session")
        if session is None:
            logger.error(
                "Group security could not run: database session missing",
                group_id=event.chat.id,
                message_id=event.message_id,
            )
            return await handler(event, data)

        try:
            security = await session.scalar(
                select(SecuritySettings).where(SecuritySettings.group_id == event.chat.id)
            )
        except Exception as exc:
            logger.exception(
                "Group security settings lookup failed",
                group_id=event.chat.id,
                message_id=event.message_id,
                error=str(exc),
            )
            return await handler(event, data)

        if security is None or not (security.anti_link or security.anti_forward_spam):
            return await handler(event, data)

        # Keep the established admin exemption, but only when Telegram confirms
        # the sender's current role. If Telegram cannot verify, do not silently
        # exempt the message from moderation.
        if sender is not None:
            try:
                member = await event.bot.get_chat_member(event.chat.id, sender.id)
                status = getattr(member.status, "value", member.status)
                if status in ("administrator", "creator"):
                    return await handler(event, data)
            except Exception as exc:
                logger.warning(
                    "Could not verify sender admin status; applying enabled protections",
                    group_id=event.chat.id,
                    user_id=sender.id,
                    error=str(exc),
                )

        try:
            violations = await check_message_violations(event, security)
        except Exception as exc:
            logger.exception(
                "Group security scan failed",
                group_id=event.chat.id,
                message_id=event.message_id,
                error=str(exc),
            )
            return await handler(event, data)

        matched = []
        if security.anti_link and "link" in violations:
            matched.append("anti_link")
        if security.anti_forward_spam and "forward_spam" in violations:
            matched.append("anti_forward")

        if not matched:
            return await handler(event, data)

        try:
            await event.delete()
            logger.info(
                "Group security deleted a message",
                group_id=event.chat.id,
                user_id=getattr(sender, "id", None),
                sender_chat_id=getattr(sender_chat, "id", None),
                message_id=event.message_id,
                protections=matched,
                edited=bool(getattr(event, "edit_date", None)),
            )
            return None
        except Exception as exc:
            # Deletion is a Telegram permission/API operation; never report it as
            # successful when Telegram rejects it. Keep the failure actionable.
            logger.error(
                "Group security matched a message but Telegram deletion failed",
                group_id=event.chat.id,
                user_id=getattr(sender, "id", None),
                message_id=event.message_id,
                protections=matched,
                error=str(exc),
                hint="Check that ALEXA is an administrator with Delete Messages permission.",
            )
            return await handler(event, data)
