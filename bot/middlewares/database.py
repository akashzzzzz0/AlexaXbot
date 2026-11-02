from typing import Any, Awaitable, Callable, Dict
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery, Update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from bot.database.models import User, Group, GroupMember, SecuritySettings, GroupDisabledCommand, InboxMessage, GroupInboxMessage
import structlog

logger = structlog.get_logger()


def _owner_inbox_preview(message: Message) -> str:
    """Store a compact media reference without changing the database schema."""
    import json
    media_fields = (
        ("photo", message.photo[-1] if message.photo else None),
        ("video", message.video), ("animation", message.animation),
        ("document", message.document), ("audio", message.audio),
        ("voice", message.voice), ("video_note", message.video_note),
        ("sticker", message.sticker),
    )
    for kind, item in media_fields:
        if item:
            return "__ALEXA_MEDIA__" + json.dumps({
                "kind": kind, "file_id": item.file_id,
                "mime_type": getattr(item, "mime_type", None),
                "caption": (message.caption or "")[:500],
            }, separators=(",", ":"))
    return (message.text or message.caption or f"[{message.content_type}]")[:1000]


class DatabaseMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        # Maintenance mode blocks ordinary users but always leaves the owner
        # able to operate the panel and turn maintenance back off.
        try:
            from bot.handlers.owner import get_maintenance_mode, is_owner
            from aiogram.types import Message, CallbackQuery
            actor = data.get("event_from_user")
            if get_maintenance_mode() and actor and not is_owner(actor.id):
                if isinstance(event, Update):
                    if event.callback_query:
                        await event.callback_query.answer("ALEXA is temporarily under maintenance.", show_alert=True)
                    elif event.message:
                        await event.message.answer("🛠 ALEXA is temporarily under maintenance. Please try again later.")
                elif isinstance(event, CallbackQuery):
                    await event.answer("ALEXA is temporarily under maintenance.", show_alert=True)
                elif isinstance(event, Message):
                    await event.answer("🛠 ALEXA is temporarily under maintenance. Please try again later.")
                return None
        except Exception as maintenance_error:
            logger.warning("Maintenance gate failed open", error=str(maintenance_error))

        from bot.database.connection import async_session_maker
        _db_ready = False
        try:
            async with async_session_maker() as session:
                data["db_session"] = session

                user = data.get("event_from_user")
                if user and not user.is_bot:
                    try:
                        db_user = await self._get_or_create_user(session, user)
                        data["db_user"] = db_user
                        # Any private message/callback proves the user has opened the bot DM.
                        private_interaction = False
                        if isinstance(event, Update):
                            private_interaction = bool(
                                (event.message and event.message.chat.type == "private")
                                or (event.callback_query and event.callback_query.message and event.callback_query.message.chat.type == "private")
                            )
                        elif isinstance(event, Message):
                            private_interaction = event.chat.type == "private"
                        elif isinstance(event, CallbackQuery) and event.message:
                            private_interaction = event.message.chat.type == "private"
                        if private_interaction:
                            db_user.dm_started = True
                        if isinstance(event, Update):
                            try:
                                from bot.services.mission_service import track_progress
                                # A savepoint prevents mission failures from leaving the
                                # update's main database session in a failed state.
                                async with session.begin_nested():
                                    if event.message and event.message.text:
                                        raw = event.message.text.strip()
                                        if raw.startswith("/"):
                                            command = raw.split(None, 1)[0].split("@", 1)[0].lower().lstrip("/")
                                            game_commands = {"dice", "rps", "quiz", "wheel", "roulette", "cards", "card", "mines", "bomb", "treasure", "numwar", "bluff", "hack", "kill", "rob"}
                                            if command in game_commands:
                                                await track_progress(session, db_user.id, "games")
                                        elif raw:
                                            await track_progress(session, db_user.id, "messages")
                                    elif event.callback_query:
                                        callback_data = event.callback_query.data or ""
                                        game_callbacks = ("game:treasure", "game:wheel", "game:cards", "game:numwar", "game:mines", "dice:daily", "rps:play:", "roulette:", "quiz_ans:")
                                        if any(callback_data.startswith(prefix) for prefix in game_callbacks):
                                            await track_progress(session, db_user.id, "games")
                            except Exception as mission_error:
                                logger.warning("Mission progress tracking failed", error=str(mission_error))
                    except Exception as e:
                        logger.warning("Could not get/create user or track mission progress", error=str(e))
                        data["db_user"] = None

                chat = None
                if isinstance(event, Update):
                    if event.message:
                        chat = event.message.chat
                    elif event.callback_query and event.callback_query.message:
                        chat = event.callback_query.message.chat
                    elif event.chat_member:
                        chat = event.chat_member.chat
                    elif event.my_chat_member:
                        chat = event.my_chat_member.chat
                elif isinstance(event, Message):
                    chat = event.chat
                elif isinstance(event, CallbackQuery) and event.message:
                    chat = event.message.chat

                if chat and chat.type in ("group", "supergroup"):
                    try:
                        db_group = await self._get_or_create_group(session, chat)
                        data["db_group"] = db_group

                        # Track the actual target of chat_member updates. This lets
                        # ALEXA remember members who join/leave without needing to
                        # enumerate the entire Telegram member list.
                        if isinstance(event, Update) and event.chat_member:
                            target = event.chat_member.new_chat_member.user
                            if target and not target.is_bot:
                                target_user = await self._get_or_create_user(session, target)
                                target_member = await self._get_or_create_member(
                                    session, chat.id, target.id
                                )
                                target_member.role = event.chat_member.new_chat_member.status
                                data["db_member"] = target_member

                        # A delivered group message proves the bot is still present.
                        # This also repairs older rows whose membership status update
                        # was missed while the bot was offline.
                        if isinstance(event, Update) and event.message and event.message.chat.type in ("group", "supergroup"):
                            db_group.is_active = True

                        # Keep the owner panel synchronized with the bot's actual
                        # Telegram membership. aiogram may expose status as an enum.
                        if isinstance(event, Update) and event.my_chat_member:
                            raw_status = event.my_chat_member.new_chat_member.status
                            bot_status = getattr(raw_status, "value", raw_status)
                            db_group.is_active = bot_status in ("member", "administrator", "creator")

                        security = await session.scalar(
                            select(SecuritySettings).where(SecuritySettings.group_id == chat.id)
                        )
                        if not security:
                            security = SecuritySettings(group_id=chat.id)
                            session.add(security)
                            await session.commit()
                            await session.refresh(security)
                        data["group_security"] = security

                        if user and not user.is_bot:
                            member = await self._get_or_create_member(session, chat.id, user.id)
                            data["db_member"] = member
                    except Exception as e:
                        logger.warning("Could not load group data from DB", error=str(e))

                # Save private messages from non-owner users for the owner-only inbox.
                # Only metadata is stored; the original message stays in the user's chat.
                if isinstance(event, Update) and event.message and event.message.chat.type == "private" and user and not user.is_bot:
                    try:
                        from bot.handlers.owner import is_owner
                        if not is_owner(user.id):
                            incoming = event.message
                            preview = _owner_inbox_preview(incoming)
                            session.add(InboxMessage(
                                user_id=user.id,
                                telegram_message_id=incoming.message_id,
                                content_type=str(incoming.content_type),
                                preview=preview[:1000],
                                is_read=False,
                            ))
                    except Exception as inbox_error:
                        logger.warning("Could not save private message to owner inbox", user_id=user.id, error=str(inbox_error))

                # Store only group messages that Telegram actually delivers to ALEXA.
                # Bot API privacy mode still determines which messages are visible.
                if isinstance(event, Update) and event.message and event.message.chat.type in ("group", "supergroup") and user and not user.is_bot:
                    try:
                        incoming = event.message
                        sender = incoming.from_user
                        already_saved = await session.scalar(select(GroupInboxMessage.id).where(
                            GroupInboxMessage.chat_id == incoming.chat.id,
                            GroupInboxMessage.telegram_message_id == incoming.message_id,
                        ))
                        if not already_saved:
                            preview = _owner_inbox_preview(incoming)
                            session.add(GroupInboxMessage(
                                chat_id=incoming.chat.id,
                                telegram_message_id=incoming.message_id,
                                sender_id=sender.id if sender else None,
                                sender_name=((sender.full_name if sender else None) or "Unknown")[:256],
                                sender_username=(sender.username[:64] if sender and sender.username else None),
                                content_type=str(incoming.content_type),
                                preview=preview[:1000],
                            ))
                    except Exception as group_inbox_error:
                        logger.warning("Could not save group message to owner inbox", chat_id=chat.id if chat else None, error=str(group_inbox_error))

                # Emergency lockdown must run before command routers, not only in the catch-all handler.
                if isinstance(event, Update) and chat and chat.type in ("group", "supergroup") and data.get("group_security") and data["group_security"].emergency_mode and user and not user.is_bot:
                    try:
                        from bot.handlers.owner import is_owner
                        is_group_admin = is_owner(user.id)
                        if not is_group_admin:
                            member_status = await event.bot.get_chat_member(chat.id, user.id)
                            is_group_admin = member_status.status in ("administrator", "creator")
                        if not is_group_admin:
                            if event.callback_query:
                                await event.callback_query.answer("Emergency lockdown is enabled in this group.", show_alert=True)
                            elif event.message:
                                try: await event.message.delete()
                                except Exception: pass
                            return None
                    except Exception as emergency_error:
                        # Do not silently swallow every group update when Telegram's
                        # membership lookup fails. Log the failure and let normal
                        # command handlers run; the next update will retry the check.
                        logger.error(
                            "Emergency lockdown membership check failed; allowing update to continue",
                            chat_id=chat.id,
                            user_id=user.id,
                            error=str(emergency_error),
                        )

                # Enforce disabled commands before routers can execute their handlers.
                if isinstance(event, Update) and event.message and event.message.text and chat and chat.type in ("group", "supergroup"):
                    raw_text = event.message.text.strip()
                    if raw_text.startswith("/"):
                        command = raw_text.split(None, 1)[0].split("@", 1)[0].lstrip("/").lower()
                        protected = {"enablecmd", "disablecmd", "disabledcmds", "owner", "settings", "security", "help", "start", "cancel"}
                        if command not in protected:
                            disabled = await session.scalar(select(GroupDisabledCommand).where(GroupDisabledCommand.group_id == chat.id, GroupDisabledCommand.command == command))
                            if disabled:
                                try: await event.message.delete()
                                except Exception: pass
                                await session.commit()
                                return None

                _db_ready = True
                try:
                    result = await handler(event, data)
                    await session.commit()
                    return result
                except Exception:
                    try:
                        await session.rollback()
                    except Exception:
                        pass
                    raise

        except Exception as db_err:
            if _db_ready:
                raise
            logger.error("DB session failed — running handler without DB", error=str(db_err))
            data["db_session"] = None
            data["db_user"] = None
            return await handler(event, data)

    async def _get_or_create_user(self, session: AsyncSession, tg_user) -> User:
        user = await session.get(User, tg_user.id)
        if not user:
            user = User(
                id=tg_user.id,
                username=tg_user.username,
                first_name=tg_user.first_name or "User",
                last_name=tg_user.last_name,
is_bot=tg_user.is_bot,
                is_premium=getattr(tg_user, "is_premium", False) or False,
                telegram_id=tg_user.id,
            )
            session.add(user)
            await session.flush()
        else:
            user.username = tg_user.username
            user.first_name = tg_user.first_name or "User"
            user.last_name = tg_user.last_name
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            last_seen = user.last_seen
            if last_seen is None:
                user.last_seen = now
            else:
                # SQLite may return naive UTC datetimes for timezone columns.
                if last_seen.tzinfo is None:
                    last_seen = last_seen.replace(tzinfo=timezone.utc)
                # Activity statistics do not need a database write per update.
                if (now - last_seen).total_seconds() >= 60:
                    user.last_seen = now
        return user

    async def _get_or_create_group(self, session: AsyncSession, chat) -> Group:
        group = await session.get(Group, chat.id)
        if not group:
            group = Group(
                id=chat.id,
                title=chat.title or "Unknown",
                username=chat.username,
                type=chat.type,
            )
            session.add(group)
            await session.flush()
        else:
            group.title = chat.title or group.title
            group.username = chat.username
        return group

    async def _get_or_create_member(
        self, session: AsyncSession, group_id: int, user_id: int
    ) -> GroupMember:
        from sqlalchemy import and_
        member = await session.scalar(
            select(GroupMember).where(
                and_(GroupMember.group_id == group_id, GroupMember.user_id == user_id)
            )
        )
        if not member:
            member = GroupMember(group_id=group_id, user_id=user_id)
            session.add(member)
            await session.flush()
        return member
