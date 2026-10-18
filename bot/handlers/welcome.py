import asyncio
from aiogram import Router, F
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import Message, ChatMemberUpdated, ChatPermissions, CallbackQuery
from aiogram.filters import ChatMemberUpdatedFilter, JOIN_TRANSITION, LEAVE_TRANSITION
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
from bot.database.models import (
    Group, GroupMember, GroupSettings, WelcomeSettings, GoodbyeSettings,
    SecuritySettings, BioLinkProtectionSetting, BioLinkProtectionConfig, CaptchaChallenge, GroupStats, User, GroupFilter, GroupBlocklist
)
from bot.services.security_service import (
    check_message_violations, is_raid_detected, create_captcha_challenge,
    contains_link, count_mentions, count_hashtags
)
from bot.services.moderation_service import mute_user, ban_user, kick_user, warn_user, log_action
from bot.services.user_service import add_xp
from bot.middlewares.i18n import get_text
from bot.utils.helpers import safe_username, mention_user
from aiogram.utils.keyboard import InlineKeyboardBuilder
from datetime import datetime, timezone
import structlog

logger = structlog.get_logger()
router = Router()


@router.message(
    F.chat.type.in_({"group", "supergroup"}),
    F.new_chat_members | F.left_chat_member,
)
async def suppress_service_messages_early(message: Message, db_session: AsyncSession, **kwargs):
    """Delete Telegram join/leave/ban service messages before broad message handlers run."""
    settings_row = await db_session.scalar(
        select(GroupSettings).where(GroupSettings.group_id == message.chat.id)
    )
    if not (settings_row and settings_row.silent_service_messages):
        # Let the normal welcome/goodbye and other handlers process this update.
        raise SkipHandler
    try:
        await message.delete()
        logger.info(
            "Deleted group join/leave service message",
            chat_id=message.chat.id,
            message_id=message.message_id,
        )
    except Exception as exc:
        logger.warning(
            "Could not delete group join/leave service message; check delete permission",
            chat_id=message.chat.id,
            message_id=message.message_id,
            error=str(exc),
        )



async def _delete_after_20_seconds(bot, chat_id: int, message_id: int):
    """Remove temporary moderation notices after 20 seconds."""
    await asyncio.sleep(20)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as exc:
        logger.debug("Temporary notice could not be deleted", chat_id=chat_id, message_id=message_id, error=str(exc))


async def _send_temporary_notice(bot, chat_id: int, text: str):
    """Send a notice that automatically disappears after 20 seconds."""
    sent = await bot.send_message(chat_id, text)
    asyncio.create_task(_delete_after_20_seconds(bot, chat_id, sent.message_id))
    return sent


_recent_welcome_keys = {}


@router.my_chat_member()
async def on_bot_membership_changed(event: ChatMemberUpdated, db_session: AsyncSession = None, **kwargs):
    """Track whether ALEXA is still in each group and request required permissions."""
    chat = event.chat
    if chat.type == "private":
        # Telegram reports a blocked/removed bot as kicked in the user's private chat.
        if db_session:
            user = await db_session.get(User, chat.id)
            new_status_private = str(getattr(event.new_chat_member.status, "value", event.new_chat_member.status))
            if user and new_status_private in ("kicked", "left"):
                user.dm_started = False
        return
    if chat.type not in ("group", "supergroup"):
        return

    old_status = str(getattr(event.old_chat_member.status, "value", event.old_chat_member.status))
    new_member = event.new_chat_member
    new_status = str(getattr(new_member.status, "value", new_member.status))
    was_added = old_status in ("left", "kicked") and new_status in ("member", "administrator")
    was_promoted = old_status != "administrator" and new_status == "administrator"
    if db_session:
        try:
            group = await db_session.get(Group, chat.id)
            if new_status in ("left", "kicked"):
                if group:
                    group.is_active = False
                    await db_session.commit()
                return
            if new_status in ("member", "administrator"):
                if not group:
                    group = Group(id=chat.id, title=chat.title or "Unknown", username=chat.username, type=chat.type, is_active=True)
                    db_session.add(group)
                else:
                    group.title = chat.title or group.title
                    group.username = chat.username
                    group.is_active = True
                if new_status in ("member", "administrator"):
                    actor = await db_session.get(User, event.from_user.id)
                    if actor:
                        actor.has_added_bot_to_group = True
                await db_session.commit()
        except Exception as exc:
            logger.warning("Could not update group membership status", group_id=chat.id, error=str(exc))
            try:
                await db_session.rollback()
            except Exception:
                pass
    if not (was_added or was_promoted):
        return

    if new_status != "administrator":
        await event.bot.send_message(
            chat.id,
            "✨ <b>ALEXA IS HERE!</b> ✨\n\n"
            "🛡️ Your group’s new moderation & protection assistant is ready to set up.\n\n"
            "👑 <b>One quick step:</b> promote ALEXA to <b>Administrator</b> and enable:\n"
            "▫️ Delete messages — locks & cleanup\n"
            "▫️ Restrict members — mute controls\n"
            "▫️ Ban users — ban/kick tools\n"
            "▫️ Invite users — invite-link tools\n"
            "📌 Pin Messages is optional.\n\n"
            "⚙️ Open <b>Group Settings → Administrators</b> to manage permissions.\n"
            "💫 Let’s keep this group safer, cleaner, and under control!"
        )
        return

    missing = []
    if not getattr(new_member, "can_delete_messages", False):
        missing.append("Delete messages")
    if not getattr(new_member, "can_restrict_members", False):
        missing.append("Restrict members (mute/ban)")
    if not getattr(new_member, "can_invite_users", False):
        missing.append("Invite users (invite-link features)")
    if missing:
        await event.bot.send_message(
            chat.id,
            "⚠️ <b>ALEXA SETUP IS ALMOST DONE</b>\n\n"
            "A few permissions are still missing:\n"
            + "\n".join(f"▫️ {item}" for item in missing)
            + "\n\n⚙️ Please open <b>Group Settings → Administrators</b> and update ALEXA’s rights."
            + "\n📌 Pin Messages is optional. Once permissions are set, protection tools can work properly."
        )
    else:
        optional = "\nPin messages is optional and only needed for pinning features."
        if not getattr(new_member, "can_pin_messages", False):
            await event.bot.send_message(
                chat.id,
                "🎉 <b>ALEXA IS READY!</b> 🎉\n\n"
                "🛡️ Core admin permissions are enabled. Moderation and protection tools are ready to configure.\n\n"
                "⚙️ Open <b>Moderation</b> or <b>Security</b> to set up this group.\n"
                "💜 Thanks for trusting ALEXA to help keep your community safe!"
                + optional,
            )
        else:
            await event.bot.send_message(
                chat.id,
                "🎉 <b>ALEXA IS READY!</b> 🎉\n\n"
                "🛡️ Core admin permissions are enabled. Moderation and protection tools are ready to configure.\n\n"
                "⚙️ Open <b>Moderation</b> or <b>Security</b> to set up this group.\n"
                "💜 Thanks for trusting ALEXA to help keep your community safe!"
            )


async def _send_welcome_message(bot, db_session, chat, user):
    """Send the configured welcome, or a default welcome when none is set."""
    now = asyncio.get_running_loop().time()
    key = (chat.id, user.id)
    last = _recent_welcome_keys.get(key, 0)
    if now - last < 20:
        return
    _recent_welcome_keys[key] = now
    # Bound the cache so long-running bots do not accumulate every join forever.
    if len(_recent_welcome_keys) > 2000:
        for old_key, stamp in list(_recent_welcome_keys.items()):
            if now - stamp > 60:
                _recent_welcome_keys.pop(old_key, None)
    ws = await db_session.scalar(select(WelcomeSettings).where(WelcomeSettings.group_id == chat.id))
    if ws and not ws.enabled:
        return
    welcome_text = (ws.message if ws and ws.message else None) or "👋 Welcome {name} to {group}!"
    replacements = {
        "{name}": user.first_name or "User",
        "{username}": f"@{user.username}" if user.username else (user.first_name or "User"),
        "{id}": str(user.id),
        "{group}": chat.title or "Group",
    }
    for key, val in replacements.items():
        welcome_text = welcome_text.replace(key, val)
    try:
        await bot.send_message(chat.id, welcome_text, parse_mode="HTML")
    except Exception as exc:
        logger.warning("Welcome message failed", chat_id=chat.id, user_id=user.id, error=str(exc))


@router.chat_member(ChatMemberUpdatedFilter(JOIN_TRANSITION))
async def on_new_member(event: ChatMemberUpdated, db_session: AsyncSession, **kwargs):
    user = event.new_chat_member.user
    chat = event.chat
    from bot.config import settings
    if user.id == settings.effective_owner_id:
        return
    group = await db_session.get(Group, chat.id)

    lang = (group.language if group else None) or "en"
    _ = lambda key, **kw: get_text(lang, key, **kw)
    security = await db_session.scalar(select(SecuritySettings).where(SecuritySettings.group_id == chat.id))

    if user.is_bot:
        if security and security.anti_bot:
            try:
                await event.bot.ban_chat_member(chat.id, user.id)
                await event.bot.unban_chat_member(chat.id, user.id)
                logger.info("Bot account rejected by anti-bot", chat_id=chat.id, user_id=user.id)
            except Exception as exc:
                logger.warning("Anti-bot could not remove joining bot", chat_id=chat.id, user_id=user.id, error=str(exc))
        return

    # Bio-link protection has its own persistent moderation switch. For groups
    # without an explicit choice yet, preserve the previous anti-link-linked behavior.
    bio_setting = await db_session.scalar(
        select(BioLinkProtectionSetting).where(BioLinkProtectionSetting.group_id == chat.id)
    )
    bio_protection_enabled = (
        bool(bio_setting.enabled) if bio_setting is not None
        else bool(security and security.anti_link)
    )
    # Telegram may not expose bios for every joining member, so this is best-effort.
    if bio_protection_enabled:
        try:
            profile = await event.bot.get_chat(user.id)
            bio = getattr(profile, "bio", None) or ""
            if bio and contains_link(bio):
                config = await db_session.scalar(
                    select(BioLinkProtectionConfig).where(BioLinkProtectionConfig.group_id == chat.id)
                )
                action = config.action if config else "mute"
                duration = config.mute_duration if config else 3600
                try:
                    if action == "ban":
                        await event.bot.ban_chat_member(chat.id, user.id)
                        await event.bot.unban_chat_member(chat.id, user.id)
                        notice = "🚫 Member removed: profile bio contains a link."
                    elif action == "mute":
                        from datetime import timedelta
                        from aiogram.types import ChatPermissions
                        until = datetime.now(timezone.utc) + timedelta(seconds=max(60, duration))
                        await event.bot.restrict_chat_member(
                            chat.id, user.id, permissions=ChatPermissions(can_send_messages=False),
                            until_date=until,
                        )
                        notice = f"🔇 Member muted for {max(1, duration // 60)} minutes because their profile bio contains a link."
                    else:
                        notice = "⚠️ Warning: profile bio contains a link. Please remove it to avoid moderation."
                    await _send_temporary_notice(event.bot, chat.id, notice)
                except Exception as action_exc:
                    logger.warning("Bio-link action failed", chat_id=chat.id, user_id=user.id, action=action, error=str(action_exc))
                logger.info("Member bio contains a link", chat_id=chat.id, user_id=user.id, action=action)
                if action in ("ban", "mute"):
                    return
        except Exception as exc:
            # Do not block normal joins when Telegram does not expose the bio.
            logger.debug(
                "Bio-link check unavailable for joining member",
                chat_id=chat.id,
                user_id=user.id,
                error=str(exc),
            )

    if security and (security.anti_raid or security.anti_mass_join):
        redis = kwargs.get("redis_client") or getattr(event.bot, "_redis_client", None)
        from bot.services.security_service import is_raid_detected
        raid = await is_raid_detected(
            redis,
            chat.id,
            threshold=security.anti_raid_threshold or 10,
            window=security.anti_raid_window or 60,
        )
        if raid:
            logger.warning("Raid/mass join detected, rejecting join", chat_id=chat.id, user_id=user.id)
            try:
                await event.bot.ban_chat_member(chat.id, user.id)
                await event.bot.unban_chat_member(chat.id, user.id)
            except Exception as exc:
                logger.warning("Raid member removal failed", chat_id=chat.id, user_id=user.id, error=str(exc))
            return

    group_settings = await db_session.scalar(
        select(GroupSettings).where(GroupSettings.group_id == chat.id)
    )
    if not (group_settings and group_settings.silent_service_messages):
        await _send_welcome_message(event.bot, db_session, chat, user)

    if security and security.captcha_enabled:
        await _handle_captcha(event, db_session, security, user, chat, _)
        return

    gs = await db_session.scalar(select(GroupStats).where(GroupStats.group_id == chat.id))
    if gs:
        gs.total_joins = (gs.total_joins or 0) + 1

    gs_settings = await db_session.scalar(select(GroupSettings).where(GroupSettings.group_id == chat.id))
    if gs_settings and gs_settings.log_join and gs_settings.log_channel_id:
        try:
            name = mention_user(user.id, safe_username(user))
            await event.bot.send_message(
                gs_settings.log_channel_id,
                f"📥 {name} joined {chat.title}",
                parse_mode="HTML",
            )
        except Exception:
            pass


async def _handle_captcha(event, db_session, security, user, chat, _):
    # Only expose captcha types with an implemented verification path. Older
    # saved "text" values are normalized to the button challenge.
    captcha_type = security.captcha_type if security.captcha_type in {"math", "button"} else "button"
    challenge = await create_captcha_challenge(
        db_session, chat.id, user.id, captcha_type, security.captcha_timeout or 60
    )
    try:
        await mute_user(event.bot, db_session, chat.id, user.id, event.bot.id, 300, "Pending captcha verification")
    except Exception:
        pass

    if captcha_type == "math":
        from bot.services.security_service import generate_math_question
        import random
        question, math_answer = generate_math_question()
        challenge.answer = math_answer
        text = _("captcha_question").format(question=question)
        builder = InlineKeyboardBuilder()
        correct_val = int(math_answer)
        options = [correct_val]
        while len(options) < 4:
            offset = random.randint(-5, 5)
            candidate = correct_val + offset
            if candidate != correct_val and candidate not in options and candidate > 0:
                options.append(candidate)
        random.shuffle(options)
        for opt in options:
            builder.button(text=str(opt), callback_data=f"captcha_verify:{chat.id}:{user.id}:{opt}")
        builder.adjust(2)
        try:
            msg = await event.bot.send_message(chat.id, text, parse_mode="HTML", reply_markup=builder.as_markup())
            challenge.message_id = msg.message_id
        except Exception as e:
            logger.error("Captcha message failed", error=str(e))
    else:
        builder = InlineKeyboardBuilder()
        builder.button(text=_("btn_verify"), callback_data=f"captcha_verify:{chat.id}:{user.id}:button")
        try:
            name = mention_user(user.id, safe_username(user))
            text = f"{name}, {_('verification_required')}"
            msg = await event.bot.send_message(chat.id, text, parse_mode="HTML", reply_markup=builder.as_markup())
            challenge.message_id = msg.message_id
        except Exception as e:
            logger.error("Captcha message failed", error=str(e))


@router.callback_query(F.data.startswith("captcha_verify:"))
async def captcha_verify(callback, db_session: AsyncSession, **kwargs):
    parts = callback.data.split(":")
    if len(parts) < 4:
        return
    group_id = int(parts[1])
    user_id = int(parts[2])
    answer = parts[3]

    if callback.from_user.id != user_id:
        await callback.answer("This captcha is not for you!", show_alert=True)
        return

    from bot.services.security_service import verify_captcha
    success = await verify_captcha(db_session, group_id, user_id, answer)

    group = await db_session.get(Group, group_id)
    lang = group.language if group else "en"
    _ = lambda key, **kw: get_text(lang, key, **kw)

    if success:
        try:
            from bot.services.moderation_service import FULL_PERMISSIONS
            await callback.bot.restrict_chat_member(
                chat_id=group_id,
                user_id=user_id,
                permissions=FULL_PERMISSIONS,
            )
        except Exception:
            pass
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.answer(_("verification_success"), show_alert=True)
    else:
        try:
            await callback.bot.ban_chat_member(chat_id=group_id, user_id=user_id)
            await callback.bot.unban_chat_member(chat_id=group_id, user_id=user_id)
        except Exception:
            pass
        await callback.answer(_("verification_failed"), show_alert=True)


@router.chat_member(ChatMemberUpdatedFilter(LEAVE_TRANSITION))
async def on_member_left(event: ChatMemberUpdated, db_session: AsyncSession, **kwargs):
    user = event.old_chat_member.user
    chat = event.chat
    if user.is_bot:
        return

    gs = await db_session.scalar(select(GroupStats).where(GroupStats.group_id == chat.id))
    if gs:
        gs.total_leaves = (gs.total_leaves or 0) + 1

    group_settings = await db_session.scalar(
        select(GroupSettings).where(GroupSettings.group_id == chat.id)
    )
    silent_service_messages = bool(group_settings and group_settings.silent_service_messages)
    goodbye = await db_session.scalar(select(GoodbyeSettings).where(GoodbyeSettings.group_id == chat.id))
    if not silent_service_messages and goodbye and goodbye.enabled and goodbye.message:
        goodbye_text = goodbye.message
        replacements = {
            "{name}": user.first_name or "User",
            "{username}": f"@{user.username}" if user.username else (user.first_name or "User"),
            "{id}": str(user.id),
            "{group}": chat.title or "Group",
        }
        for key, value in replacements.items():
            goodbye_text = goodbye_text.replace(key, value)
        try:
            await event.bot.send_message(chat.id, goodbye_text, parse_mode="HTML")
        except Exception as exc:
            logger.warning("Goodbye message failed", error=str(exc))

    group = await db_session.get(Group, chat.id)
    if not group:
        return
    security = await db_session.scalar(select(SecuritySettings).where(SecuritySettings.group_id == chat.id))
    if security and security.anti_mass_leave:
        from bot.services.security_service import record_member_leave
        if record_member_leave(chat.id, security.anti_raid_threshold or 10, security.anti_raid_window or 60):
            security.emergency_mode = True
            logger.warning("Mass leave detected; emergency lockdown enabled", chat_id=chat.id)
    gs_settings = await db_session.scalar(select(GroupSettings).where(GroupSettings.group_id == chat.id))
    if gs_settings and gs_settings.log_leave and gs_settings.log_channel_id:
        try:
            name = safe_username(user)
            await event.bot.send_message(
                gs_settings.log_channel_id,
                f"📤 {name} left {chat.title}",
            )
        except Exception:
            pass


@router.message(F.chat.type.in_({"group", "supergroup"}), F.new_chat_members)
async def group_service_message_welcome(message: Message, db_session: AsyncSession, **kwargs):
    """Fallback for Telegram service messages when chat_member updates are absent."""
    settings_row = await db_session.scalar(
        select(GroupSettings).where(GroupSettings.group_id == message.chat.id)
    )
    if settings_row and settings_row.silent_service_messages:
        try:
            await message.delete()
        except Exception as exc:
            logger.debug("Could not delete join service message", chat_id=message.chat.id, error=str(exc))
        return
    for joined_user in message.new_chat_members or []:
        if joined_user.is_bot:
            continue
        await _send_welcome_message(message.bot, db_session, message.chat, joined_user)


@router.message(F.chat.type.in_({"group", "supergroup"}))
async def group_message_handler(message: Message, db_session: AsyncSession, db_user=None, db_group=None, group_security=None, _=None, **kwargs):
    # This broad handler is registered before the command routers. Let slash
    # commands continue to the actual command handlers instead of consuming
    # them here; otherwise /start, /help, moderation and game commands never run.
    if message.text and message.text.lstrip().startswith("/"):
        raise SkipHandler

    # Resolve group/security directly as a fallback if middleware did not inject them.
    # This prevents the link-protection handler from silently skipping messages.
    if not db_group:
        db_group = await db_session.get(Group, message.chat.id)
    if not db_group:
        return
    if message.new_chat_members or message.left_chat_member:
        settings_row = await db_session.scalar(
            select(GroupSettings).where(GroupSettings.group_id == message.chat.id)
        )
        if settings_row and settings_row.silent_service_messages:
            try:
                await message.delete()
            except Exception as exc:
                logger.debug(
                    "Could not delete join/leave/ban service message",
                    chat_id=message.chat.id,
                    message_id=message.message_id,
                    error=str(exc),
                )
            return
    if not group_security:
        group_security = await db_session.scalar(
            select(SecuritySettings).where(SecuritySettings.group_id == message.chat.id)
        )
    if not group_security:
        group_security = SecuritySettings(group_id=message.chat.id)
        db_session.add(group_security)
        try:
            await db_session.flush()
        except Exception:
            await db_session.rollback()
            logger.exception("Could not initialize group security settings", group_id=message.chat.id)
            return
    # Emergency lockdown is enforced for every incoming group message.
    # Group admins/creator are exempt; all other messages are deleted while ON.
    if group_security.emergency_mode and message.from_user:
        try:
            sender = await message.bot.get_chat_member(message.chat.id, message.from_user.id)
            sender_status = str(getattr(sender.status, "value", sender.status))
            if sender_status not in ("administrator", "creator", "owner"):
                await message.delete()
                logger.info(
                    "Emergency lockdown deleted non-admin message",
                    chat_id=message.chat.id,
                    user_id=message.from_user.id,
                )
                return
        except Exception as exc:
            # If Telegram cannot verify the sender role, avoid accidentally
            # deleting an administrator's message; log so permissions can be checked.
            logger.warning(
                "Emergency lockdown could not verify sender permissions",
                chat_id=message.chat.id,
                user_id=message.from_user.id,
                error=str(exc),
            )
            return

    # Ensure moderation has a persisted user even if DatabaseMiddleware could
    # not inject db_user. Warning rows and mute state depend on this user record.
    if message.from_user and not message.from_user.is_bot and db_user is None:
        try:
            db_user = await db_session.get(User, message.from_user.id)
            if db_user is None:
                db_user = User(
                    id=message.from_user.id,
                    telegram_id=message.from_user.id,
                    first_name=message.from_user.first_name or "User",
                    last_name=message.from_user.last_name,
                    username=message.from_user.username,
                    is_bot=False,
                )
                db_session.add(db_user)
                await db_session.flush()
        except Exception as exc:
            logger.warning("Could not ensure message sender database user", chat_id=message.chat.id, user_id=message.from_user.id, error=str(exc))
            db_user = None

    # Bot accounts are not persisted as regular users by DatabaseMiddleware.
    # Keep locking other bots when configured, but never delete ALEXA's own
    # outbound messages (including its official links/promotions).
    if message.from_user and message.from_user.is_bot:
        is_own_bot_message = message.from_user.id == message.bot.id
        if group_security.lock_bots and not is_own_bot_message:
            try:
                await message.delete()
            except Exception as exc:
                logger.debug("Could not delete bot message under bot lock", error=str(exc))
        return
    # Do not return early when a regular user record is missing: content locks
    # must still be enforced for every incoming group message.

    # Enforce each content lock independently before other moderation branches.
    # This makes a single lock affect only its matching content type; Lock ALL
    # simply enables every field in the same set. Slash commands were routed to
    # their command handlers above and are intentionally not treated as locked text.
    if message.from_user:
        content = message.text or message.caption or ""
        locked_reason = None
        if group_security.lock_text and message.text and not message.caption:
            locked_reason = "text"
        elif group_security.lock_links and contains_link(content):
            locked_reason = "links"
        elif group_security.lock_photos and message.photo:
            locked_reason = "photos"
        elif group_security.lock_videos and (message.video or message.video_note):
            locked_reason = "videos"
        elif group_security.lock_gifs and message.animation:
            locked_reason = "gifs"
        elif group_security.lock_audio and message.audio:
            locked_reason = "audio"
        elif group_security.lock_voice and message.voice:
            locked_reason = "voice"
        elif group_security.lock_documents and message.document:
            locked_reason = "documents"
        elif group_security.lock_polls and message.poll:
            locked_reason = "polls"
        elif group_security.lock_games and (message.dice or message.game):
            locked_reason = "games"
        elif group_security.lock_forwards and message.forward_origin:
            locked_reason = "forwards"
        elif group_security.lock_contacts and message.contact:
            locked_reason = "contacts"
        elif group_security.lock_locations and (message.location or message.venue):
            locked_reason = "locations"
        elif group_security.lock_stickers and message.sticker:
            sticker_approval = await db_session.scalar(
                select(GroupMember).where(
                    GroupMember.group_id == db_group.id,
                    GroupMember.user_id == message.from_user.id,
                )
            )
            if not (sticker_approval and sticker_approval.approved_sticker):
                locked_reason = "stickers"
        elif group_security.lock_inline and (
            (message.reply_markup and message.reply_markup.inline_keyboard)
            or message.via_bot
        ):
            locked_reason = "inline"
        elif group_security.lock_mentions and count_mentions(content) > 0:
            locked_reason = "mentions"
        elif group_security.lock_hashtags and count_hashtags(content) > 0:
            locked_reason = "hashtags"

        if locked_reason:
            try:
                await message.delete()
                logger.info(
                    "Content lock deleted message",
                    chat_id=message.chat.id,
                    user_id=message.from_user.id,
                    lock_type=locked_reason,
                )
            except Exception as exc:
                logger.warning(
                    "Content lock could not delete message; check bot Delete Messages permission",
                    chat_id=message.chat.id,
                    user_id=message.from_user.id,
                    lock_type=locked_reason,
                    error=str(exc),
                )
            return

    # When Bio Link Protection is enabled, also moderate links posted by existing
    # members: delete each link message, issue persistent warnings, and mute on 3.
    message_text = message.text or message.caption or ""
    if message.from_user and message_text and contains_link(message_text):
        bio_setting = await db_session.scalar(
            select(BioLinkProtectionSetting).where(
                BioLinkProtectionSetting.group_id == message.chat.id
            )
        )
        bio_protection_enabled = (
            bool(bio_setting.enabled) if bio_setting is not None
            else bool(group_security.anti_link)
        )
        if bio_protection_enabled:
            is_admin = False
            try:
                member = await message.bot.get_chat_member(message.chat.id, message.from_user.id)
                is_admin = member.status in ("administrator", "creator", "owner")
            except Exception as exc:
                logger.debug("Could not verify link sender role", error=str(exc), user_id=message.from_user.id)
            approved_row = await db_session.scalar(
                select(GroupMember).where(
                    GroupMember.group_id == db_group.id,
                    GroupMember.user_id == message.from_user.id,
                )
            )
            # /approvebio exempts this member from Bio Link Protection: do not
            # delete their messages or count bio-link warnings again.
            if not is_admin and not (approved_row and approved_row.is_whitelisted):
                try:
                    await message.delete()
                except Exception as exc:
                    logger.warning("Could not delete link message", error=str(exc), group_id=message.chat.id)
                if db_user:
                    count, reached = await warn_user(
                        db_session, db_group.id, db_user.id, message.bot.id,
                        "Posting links (Bio Link Protection)", max_warns=3
                    )
                    try:
                        await db_session.flush()
                    except Exception:
                        pass
                    if reached:
                        muted = await mute_user(
                            message.bot, db_session, db_group.id, db_user.id,
                            message.bot.id, 3600, "3 link warnings reached"
                        )
                        notice = (
                            f"🚫 Link removed. {message.from_user.first_name or 'Member'} reached "
                            f"3/3 warnings and has been muted for 1 hour."
                            if muted else
                            f"⚠️ Link removed. Member reached {count}/3 warnings, but I couldn't apply the mute. "
                            "Please check ALEXA's Restrict Members permission."
                        )
                    else:
                        notice = (
                            f"⚠️ Link removed. Warning {count}/3. "
                            "At 3 warnings, the member will be muted for 1 hour."
                        )
                    try:
                        await message.bot.send_message(message.chat.id, notice)
                    except Exception:
                        pass
                return


    # Bio-link scan for existing members on each message, similar to the standalone
    # BioLink Protector flow. Telegram Bot API may not expose every user's bio,
    # so failures are logged and must never block normal group messages.
    if message.from_user and not message.from_user.is_bot:
        bio_setting = await db_session.scalar(
            select(BioLinkProtectionSetting).where(
                BioLinkProtectionSetting.group_id == message.chat.id
            )
        )
        bio_protection_enabled = (
            bool(bio_setting.enabled) if bio_setting is not None
            else bool(group_security.anti_link)
        )
        if bio_protection_enabled:
            try:
                member_status = await message.bot.get_chat_member(
                    message.chat.id, message.from_user.id
                )
                is_admin = member_status.status in ("administrator", "creator", "owner")
                if not is_admin:
                    member_row = await db_session.scalar(
                        select(GroupMember).where(
                            GroupMember.group_id == db_group.id,
                            GroupMember.user_id == message.from_user.id,
                        )
                    )
                    if not (member_row and member_row.is_whitelisted):
                        profile = await message.bot.get_chat(message.from_user.id)
                        bio = getattr(profile, "bio", None) or ""
                        if bio and contains_link(bio):
                            config = await db_session.scalar(
                                select(BioLinkProtectionConfig).where(
                                    BioLinkProtectionConfig.group_id == db_group.id
                                )
                            )
                            action = config.action if config else "mute"
                            duration = config.mute_duration if config else 3600
                            try:
                                await message.delete()
                            except Exception:
                                pass
                            if action in ("warn", "warn_mute", "warn_ban"):
                                if db_user:
                                    count, reached = await warn_user(
                                        db_session, db_group.id, db_user.id, message.bot.id,
                                        "Link found in profile bio (Bio Link Protection)", max_warns=3
                                    )
                                    if reached and action == "warn_mute":
                                        muted = await mute_user(
                                            message.bot, db_session, db_group.id, db_user.id,
                                            message.bot.id, duration, "3 bio-link warnings reached"
                                        )
                                        notice = (
                                            f"🔇 Bio-link warning limit reached (3/3). Member muted for {max(1, duration // 60)} minutes."
                                            if muted else
                                            "⚠️ Bio-link warning limit reached (3/3), but mute failed. Check ALEXA's Restrict Members permission."
                                        )
                                    elif reached and action == "warn_ban":
                                        banned = await ban_user(
                                            message.bot, db_session, db_group.id, db_user.id,
                                            message.bot.id, "3 bio-link warnings reached"
                                        )
                                        notice = (
                                            "🚫 Bio-link warning limit reached (3/3). Member banned."
                                            if banned else
                                            "⚠️ Bio-link warning limit reached (3/3), but ban failed. Check ALEXA's Ban Users permission."
                                        )
                                    else:
                                        next_action = "muted" if action == "warn_mute" else "banned" if action == "warn_ban" else "moderated"
                                        notice = f"⚠️ Message removed. Bio-link warning {count}/3. At 3 warnings, the member will be {next_action}."
                                else:
                                    notice = "⚠️ Message removed because a link was found in your bio. Please remove it."
                            elif action == "mute":
                                muted = False
                                if db_user:
                                    muted = await mute_user(
                                        message.bot, db_session, db_group.id, db_user.id,
                                        message.bot.id, duration, "Link found in profile bio"
                                    )
                                notice = (
                                    f"🔇 Message removed. Member muted for {max(1, duration // 60)} minutes because their bio contains a link."
                                    if muted else
                                    "⚠️ Message removed because a link was found in the member's bio, but mute failed. Check ALEXA's Restrict Members permission."
                                )
                            else:
                                banned = False
                                if db_user:
                                    banned = await ban_user(
                                        message.bot, db_session, db_group.id, db_user.id,
                                        message.bot.id, "Link found in profile bio"
                                    )
                                notice = "🚫 Member banned because their bio contains a link." if banned else "⚠️ Message removed, but ban failed. Check ALEXA's Ban Users permission."
                            try:
                                await message.bot.send_message(message.chat.id, notice)
                            except Exception:
                                pass
                            return
            except Exception as exc:
                logger.debug(
                    "Bio-link scan unavailable for message sender",
                    chat_id=message.chat.id,
                    user_id=message.from_user.id,
                    error=str(exc),
                )

    # Persistent keyword blocklist and auto-reply filters configured by group admins.
    text_content = (message.text or message.caption or "").lower()
    if text_content:
        blocked_rows = (await db_session.execute(select(GroupBlocklist).where(GroupBlocklist.group_id == db_group.id))).scalars().all()
        if any(row.value and row.value.lower() in text_content for row in blocked_rows):
            try:
                await message.delete()
            except Exception:
                pass
            return
        filter_rows = (await db_session.execute(select(GroupFilter).where(GroupFilter.group_id == db_group.id))).scalars().all()
        for filter_row in filter_rows:
            target_id = getattr(filter_row, "target_user_id", None)
            target_username = (getattr(filter_row, "target_username", None) or "").lstrip("@").lower()
            target_matches = (
                (target_id is None and not target_username)
                or (message.from_user and target_id is not None and target_id == message.from_user.id)
                or (message.from_user and target_username and (message.from_user.username or "").lower() == target_username)
            )
            if filter_row.keyword and filter_row.keyword.lower() in text_content and target_matches:
                try:
                    if getattr(filter_row, "response_type", "text") == "sticker":
                        await message.answer_sticker(filter_row.response)
                    else:
                        await message.answer(filter_row.response, parse_mode=None)
                except Exception as exc:
                    logger.warning("Group filter response failed", error=str(exc), group_id=db_group.id)
                break

    approved_sticker_user = False
    if db_user:
        member_row = await db_session.scalar(select(GroupMember).where(
            GroupMember.group_id == db_group.id, GroupMember.user_id == db_user.id
        ))
        approved_sticker_user = bool(member_row and member_row.approved_sticker)

    # /approve remains a sticker-lock exemption for individual lock settings,
    # but Lock All must apply every content lock to every regular member,
    # including members previously approved for stickers.
    if approved_sticker_user:
        content_lock_fields = (
            "lock_text", "lock_links", "lock_photos", "lock_videos", "lock_gifs",
            "lock_audio", "lock_voice", "lock_documents", "lock_polls", "lock_games",
            "lock_bots", "lock_forwards", "lock_contacts", "lock_locations",
            "lock_stickers", "lock_inline", "lock_mentions", "lock_hashtags",
        )
        if all(bool(getattr(group_security, field, False)) for field in content_lock_fields):
            approved_sticker_user = False

    violations = await check_message_violations(message, group_security, approved_sticker_user=approved_sticker_user)
    # If Bio Link Protection is explicitly enabled, /approvebio also exempts
    # that member from the generic link violation generated by the same setting.
    # Content locks (locked_link) remain enforceable.
    if message.from_user and db_user:
        bio_setting = await db_session.scalar(
            select(BioLinkProtectionSetting).where(
                BioLinkProtectionSetting.group_id == db_group.id
            )
        )
        if bio_setting is not None and bio_setting.enabled:
            approved_row = await db_session.scalar(
                select(GroupMember).where(
                    GroupMember.group_id == db_group.id,
                    GroupMember.user_id == message.from_user.id,
                )
            )
            if approved_row and approved_row.is_whitelisted:
                violations = [v for v in violations if v != "link"]
    if violations:
        # A content lock means the content type is prohibited; delete it rather
        # than applying a configurable spam punishment such as mute/ban.
        if any(reason.startswith("locked_") for reason in violations):
            action = "delete"
        elif not db_user:
            # Without a persisted user record, safely delete the violating message
            # rather than attempting warn/mute/ban actions that require a DB user.
            action = "delete"
        else:
            action = (group_security.flood_action if "flood" in violations else group_security.spam_action) or "delete"
        if action == "delete":
            try:
                await message.delete()
            except Exception:
                pass
        elif action == "warn":
            gs = await db_session.scalar(select(GroupSettings).where(GroupSettings.group_id == db_group.id))
            max_warns = gs.max_warns if gs else 3
            count, reached = await warn_user(db_session, db_group.id, db_user.id, message.bot.id, violations[0], max_warns)
            try:
                await message.delete()
            except Exception:
                pass
            if reached:
                strike_action = (gs.warn_action if gs else "mute") or "mute"
                if strike_action == "mute":
                    await mute_user(message.bot, db_session, db_group.id, db_user.id, message.bot.id, 3600, "Warning limit reached")
                elif strike_action == "ban":
                    await ban_user(message.bot, db_session, db_group.id, db_user.id, message.bot.id, "Warning limit reached")
                elif strike_action == "kick":
                    await kick_user(message.bot, db_session, db_group.id, db_user.id, message.bot.id, "Warning limit reached")
        elif action in ("mute", "ban", "kick"):
            try:
                await message.delete()
            except Exception:
                pass
            if action == "mute":
                await mute_user(message.bot, db_session, db_group.id, db_user.id, message.bot.id, 3600, violations[0])
            elif action == "ban":
                await ban_user(message.bot, db_session, db_group.id, db_user.id, message.bot.id, violations[0])
            elif action == "kick":
                await kick_user(message.bot, db_session, db_group.id, db_user.id, message.bot.id, violations[0])
        return

    # User-dependent activity/XP tracking below needs a persisted user row.
    if not db_user:
        return

    flood_detected = kwargs.get("flood_detected", False)
    if flood_detected:
        action = group_security.flood_action or "mute"
        try:
            await message.delete()
        except Exception:
            pass
        if action == "mute":
            await mute_user(message.bot, db_session, db_group.id, db_user.id, message.bot.id, 300, "Flood")
        elif action == "ban":
            await ban_user(message.bot, db_session, db_group.id, db_user.id, message.bot.id, "Flood")
        elif action == "kick":
            await kick_user(message.bot, db_session, db_group.id, db_user.id, message.bot.id, "Flood")
        elif action == "warn":
            gs = await db_session.scalar(select(GroupSettings).where(GroupSettings.group_id == db_group.id))
            max_warns = gs.max_warns if gs else 3
            count, reached = await warn_user(db_session, db_group.id, db_user.id, message.bot.id, "Flood", max_warns)
            if reached:
                strike_action = (gs.warn_action if gs else "mute") or "mute"
                if strike_action == "mute": await mute_user(message.bot, db_session, db_group.id, db_user.id, message.bot.id, 3600, "Warning limit reached")
                elif strike_action == "ban": await ban_user(message.bot, db_session, db_group.id, db_user.id, message.bot.id, "Warning limit reached")
                elif strike_action == "kick": await kick_user(message.bot, db_session, db_group.id, db_user.id, message.bot.id, "Warning limit reached")
        return

    ws_autodel = await db_session.scalar(select(WelcomeSettings).where(WelcomeSettings.group_id == db_group.id))
    if ws_autodel and (ws_autodel.delete_after or 0) > 0:
        async def _delete_later(bot, chat_id, message_id, delay):
            await asyncio.sleep(delay)
            try:
                await bot.delete_message(chat_id, message_id)
            except Exception:
                pass
        asyncio.create_task(_delete_later(message.bot, message.chat.id, message.message_id, ws_autodel.delete_after))

    from sqlalchemy import and_
    member = await db_session.scalar(
        select(GroupMember).where(
            and_(GroupMember.group_id == db_group.id, GroupMember.user_id == db_user.id)
        )
    )
    if member:
        member.message_count = (member.message_count or 0) + 1
        from datetime import datetime, timezone
        member.last_activity = datetime.now(timezone.utc)

    gs_settings = await db_session.scalar(select(GroupSettings).where(GroupSettings.group_id == db_group.id))
    if gs_settings and gs_settings.xp_enabled:
        from bot.config import settings
        _, _, leveled_up = await add_xp(db_session, db_user.id, settings.ACTIVITY_XP_PER_MSG)
        if leveled_up:
            from bot.services.user_service import get_or_create_economy
            eco = await get_or_create_economy(db_session, db_user.id)
            try:
                name = mention_user(db_user.id, safe_username(db_user))
                await message.answer(
                    _("level_up").format(name=name, level=eco.level),
                    parse_mode="HTML",
                )
            except Exception:
                pass

    gs_stats = await db_session.scalar(select(GroupStats).where(GroupStats.group_id == db_group.id))
    if not gs_stats:
        gs_stats = GroupStats(group_id=db_group.id, total_messages=1)
        db_session.add(gs_stats)
    else:
        gs_stats.total_messages = (gs_stats.total_messages or 0) + 1
