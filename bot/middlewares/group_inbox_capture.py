from __future__ import annotations

import json
import logging
from aiogram import BaseMiddleware
from aiogram.types import Message
from sqlalchemy import delete, desc, select

from bot.database.connection import async_session_maker
from bot.database.models import Group, GroupInboxMessage, InboxMessage, User

logger = logging.getLogger(__name__)


class GroupInboxCaptureMiddleware(BaseMiddleware):
    """Capture group messages Telegram actually delivers to ALEXA for the owner Mini App."""

    async def __call__(self, handler, event, data):
        if isinstance(event, Message):
            try:
                if event.chat.type in ("group", "supergroup"):
                    await self._capture(event)
                elif event.chat.type == "private":
                    await self._capture_private(event)
            except Exception:
                # Owner inbox capture must never interrupt normal bot handlers.
                logger.exception("Could not capture message for owner inbox")
        return await handler(event, data)

    async def _capture_private(self, message: Message) -> None:
        sender = message.from_user
        if not sender or sender.is_bot:
            return
        async with async_session_maker() as session:
            user = await session.get(User, sender.id)
            if not user or not user.dm_started:
                return
            existing = await session.scalar(
                select(InboxMessage.id).where(
                    InboxMessage.user_id == sender.id,
                    InboxMessage.telegram_message_id == message.message_id,
                )
            )
            if existing:
                return
            content_type, preview = _message_preview(message)
            session.add(InboxMessage(
                user_id=sender.id,
                telegram_message_id=message.message_id,
                content_type=content_type,
                preview=preview,
                is_read=False,
            ))
            await session.commit()

    async def _capture(self, message: Message) -> None:
        chat_id = message.chat.id
        sender = message.from_user

        content_type, preview = _message_preview(message)
        async with async_session_maker() as session:
            group = await session.get(Group, chat_id)
            if not group or not group.is_active:
                return
            existing = await session.scalar(
                select(GroupInboxMessage.id).where(
                    GroupInboxMessage.chat_id == chat_id,
                    GroupInboxMessage.telegram_message_id == message.message_id,
                )
            )
            if existing:
                return
            session.add(GroupInboxMessage(
                chat_id=chat_id,
                telegram_message_id=message.message_id,
                sender_id=sender.id if sender else None,
                sender_name=sender.full_name if sender else (message.sender_chat.title if message.sender_chat else "Telegram"),
                sender_username=sender.username if sender else None,
                content_type=content_type,
                preview=preview,
            ))
            await session.flush()

            # Keep the owner Mini App history bounded to the latest 500 messages per group.
            old_ids = (await session.scalars(
                select(GroupInboxMessage.id)
                .where(GroupInboxMessage.chat_id == chat_id)
                .order_by(desc(GroupInboxMessage.id))
                .offset(500)
            )).all()
            if old_ids:
                await session.execute(
                    delete(GroupInboxMessage).where(GroupInboxMessage.id.in_(old_ids))
                )
            await session.commit()


def _message_preview(message: Message) -> tuple[str, str]:
    """Return a display type and either text or a private media reference."""
    if message.video_chat_started:
        return "voice_chat_started", "Voice chat started"
    if message.video_chat_ended:
        return "voice_chat_ended", "Voice chat ended"
    if message.video_chat_scheduled:
        return "voice_chat_scheduled", "Voice chat scheduled"
    if message.video_chat_participants_invited:
        invited = message.video_chat_participants_invited.users or []
        names = [user.full_name for user in invited if user]
        return "voice_chat_invited", "Voice chat invitation: " + (", ".join(names) if names else "users invited")

    media = None
    kind = None
    if message.photo:
        media, kind = message.photo[-1], "photo"
    elif message.video:
        media, kind = message.video, "video"
    elif message.animation:
        media, kind = message.animation, "animation"
    elif message.video_note:
        media, kind = message.video_note, "video_note"
    elif message.voice:
        media, kind = message.voice, "voice"
    elif message.audio:
        media, kind = message.audio, "audio"
    elif message.document:
        media, kind = message.document, "document"
    elif message.sticker:
        media, kind = message.sticker, "sticker"

    if media is not None:
        payload = {
            "file_id": media.file_id,
            "kind": kind,
            "mime_type": getattr(media, "mime_type", None),
            "caption": (message.caption or "")[:1000],
        }
        return kind, "__ALEXA_MEDIA__" + json.dumps(payload, ensure_ascii=False)

    if message.text:
        return "text", message.text[:4000]
    if message.caption:
        return "caption", message.caption[:4000]
    return "service", "[Telegram service message]"
