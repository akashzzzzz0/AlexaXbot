from __future__ import annotations

import logging
from aiogram import BaseMiddleware
from aiogram.types import Message
from sqlalchemy import delete, desc, select

from bot.database.connection import async_session_maker
from bot.database.models import Group, GroupInboxMessage

logger = logging.getLogger(__name__)


class GroupInboxCaptureMiddleware(BaseMiddleware):
    """Capture group messages Telegram actually delivers to ALEXA for the owner Mini App."""

    async def __call__(self, handler, event, data):
        if isinstance(event, Message) and event.chat.type in ("group", "supergroup"):
            try:
                await self._capture(event)
            except Exception:
                # Owner inbox capture must never interrupt normal moderation handlers.
                logger.exception("Could not capture group message for owner inbox")
        return await handler(event, data)

    async def _capture(self, message: Message) -> None:
        chat_id = message.chat.id
        sender = message.from_user

        if message.video_chat_started:
            content_type, preview = "voice_chat_started", "Voice chat started"
        elif message.video_chat_ended:
            content_type, preview = "voice_chat_ended", "Voice chat ended"
        elif message.video_chat_scheduled:
            content_type, preview = "voice_chat_scheduled", "Voice chat scheduled"
        elif message.video_chat_participants_invited:
            invited = message.video_chat_participants_invited.users or []
            names = [u.full_name for u in invited if u]
            content_type = "voice_chat_invited"
            preview = "Voice chat invitation: " + (", ".join(names) if names else "users invited")
        elif message.text:
            content_type, preview = "text", message.text
        elif message.caption:
            content_type, preview = "caption", message.caption
        elif message.photo:
            content_type, preview = "photo", "[Photo]" + ((" " + message.caption) if message.caption else "")
        elif message.video:
            content_type, preview = "video", "[Video]" + ((" " + message.caption) if message.caption else "")
        elif message.document:
            content_type, preview = "document", "[File]" + ((" " + message.caption) if message.caption else "")
        elif message.voice:
            content_type, preview = "voice", "[Voice message]"
        elif message.audio:
            content_type, preview = "audio", "[Audio]"
        elif message.sticker:
            content_type, preview = "sticker", "[Sticker]"
        else:
            content_type, preview = "service", "[Telegram service message]"

        preview = (preview or "")[:4000]
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
