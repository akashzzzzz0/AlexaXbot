import asyncio
from aiogram import Router, F
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import Message, ChatMemberUpdated, ChatPermissions, CallbackQuery
from aiogram.filters import ChatMemberUpdatedFilter, JOIN_TRANSITION, LEAVE_TRANSITION, Command, Command
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, update, func, delete
from sqlalchemy.exc import IntegrityError
from bot.database.models import (
    Group, GroupMember, GroupSettings, WelcomeSettings, GoodbyeSettings,
    SecuritySettings, BioLinkProtectionSetting, BioLinkProtectionConfig, CaptchaChallenge, GroupStats, User, GroupFilter, GroupBlocklist, ActionLog, DailyGroupMessageStat, Warning
)
from bot.services.security_service import (
    check_message_violations, is_raid_detected, create_captcha_challenge,
    contains_link, contains_non_alexa_link, count_mentions, count_hashtags,
    is_nsfw_sticker
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

async def _insert_or_get(session: AsyncSession, candidate, model, *conditions):
    try:
        async with session.begin_nested():
            session.add(candidate)
            await session.flush()
        return candidate, True
    except IntegrityError:
        existing = await session.scalar(select(model).where(*conditions))
        if existing is None:
            raise
        return existing, False




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



async def _channelguard_profile_links(bot, user_id: int):
    """Return actual URLs in the user's bio or linked personal-channel description.

    A connected channel by itself is not a violation. Telegram may hide these
    profile fields, so callers must treat an unavailable profile as unverifiable.
    """
    profile = await bot.get_chat(user_id)
    bio = getattr(profile, "bio", None)
    available = bio is not None
    evidence = []
    if bio and contains_non_alexa_link(str(bio)):
        evidence.append(("bio", str(bio)))

    linked_channel = getattr(profile, "personal_chat", None)
    if linked_channel is not None:
        available = True
        channel_info = linked_channel
        linked_id = getattr(linked_channel, "id", None)
        if linked_id is not None:
            try:
                channel_info = await bot.get_chat(linked_id)
            except Exception:
                # The profile may expose a partial Chat object; inspect any
                # description already present without treating its existence
                # or username as a violation.
                channel_info = linked_channel
        description = (
            getattr(channel_info, "description", None)
            or getattr(linked_channel, "description", None)
            or ""
        )
        if description and contains_non_alexa_link(str(description)):
            evidence.append(("connected_channel_description", str(description)))
    return evidence, available


async def _channelguard_warn_and_mute(bot, db_session: AsyncSession, group_id: int, user, display_name: str):
    """Record one ChannelGuard warning and apply the fixed 3-warning/1-hour rule."""
    user_id = user.id
    stored_user = await db_session.get(User, user_id)
    if stored_user is None:
        stored_user, _created = await _insert_or_get(
            db_session,
            User(
                id=user_id,
                telegram_id=user_id,
                first_name=getattr(user, "first_name", None) or display_name or "User",
                last_name=getattr(user, "last_name", None),
                username=getattr(user, "username", None),
                is_bot=False,
            ),
            User,
            User.id == user_id,
        )
        await db_session.flush()
    member = await db_session.scalar(
        select(GroupMember).where(
            GroupMember.group_id == group_id,
            GroupMember.user_id == user_id,
        )
    )
    if member is None:
        member, _created = await _insert_or_get(
            db_session,
            GroupMember(group_id=group_id, user_id=user_id),
            GroupMember,
            GroupMember.group_id == group_id,
            GroupMember.user_id == user_id,
        )
    if member.is_whitelisted:
        return None, 0

    channelguard_count = await db_session.scalar(
        select(func.count()).select_from(Warning).where(
            Warning.group_id == group_id,
            Warning.user_id == user_id,
            Warning.reason.like("ChannelGuard:%"),
        )
    ) or 0
    count = int(channelguard_count) + 1
    db_session.add(
        Warning(
            group_id=group_id,
            user_id=user_id,
            admin_id=bot.id,
            reason="ChannelGuard: profile link detected",
        )
    )
    member.warn_count = (member.warn_count or 0) + 1
    await log_action(
        db_session,
        group_id,
        "warn",
        user_id,
        bot.id,
        {"reason": "ChannelGuard: profile link detected"},
    )
    reached = count >= 3
    if reached:
        muted = await mute_user(
            bot,
            db_session,
            group_id,
            user_id,
            bot.id,
            3600,
            "Bio-link warning limit reached",
        )
        await db_session.commit()
        return ("muted" if muted else "mute_failed"), count

    await db_session.commit()
    return "warned", count


def _channelguard_notice(user_id: int, display_name: str, state: str, count: int):
    user_link = mention_user(user_id, display_name or f"User {user_id}")
    if state == "warned":
        return (
            f"⚠️ Warning {user_link}, please remove the link from your bio or connected channel. "
            f"Warning: {min(count, 3)}/3",
            None,
        )
    if state == "muted":
        return (
            f"🔇 {user_link} has been muted for 1 hour after reaching 3/3 warnings.\n"
            "Remove the link from your bio or connected channel, then tap Unmute.",
            _bio_admin_action_keyboard,
        )
    if state == "mute_failed":
        return (
            f"⚠️ {user_link} reached 3/3 ChannelGuard warnings, but the 1-hour mute failed. "
            "Check ALEXA's Restrict Members permission.",
            None,
        )
    return None, None


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
                    group, _created = await _insert_or_get(
                        db_session,
                        Group(id=chat.id, title=chat.title or "Unknown", username=chat.username, type=chat.type, is_active=True),
                        Group, Group.id == chat.id,
                    )
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
    from html import escape
    display_name = " ".join(
        part for part in (user.first_name, user.last_name) if part
    ) or f"User#{user.id}"
    clickable_name = mention_user(user.id, display_name)
    replacements = {
        # Both common name placeholders must be real Telegram mentions,
        # so existing custom welcome templates become tappable without edits.
        "{name}": clickable_name,
        "{mention}": clickable_name,
        "{username}": escape(f"@{user.username}" if user.username else display_name),
        "{id}": str(user.id),
        "{group}": escape(chat.title or "Group"),
    }
    for key, val in replacements.items():
        welcome_text = welcome_text.replace(key, val)
    try:
        await bot.send_message(chat.id, welcome_text, parse_mode="HTML")
    except Exception as exc:
        logger.warning("Welcome message failed", chat_id=chat.id, user_id=user.id, error=str(exc))

    # Send a private welcome only to this member if ALEXA successfully sent
    # them the join-request DM button for this exact group and they activated DM.
    # Telegram does not allow bots to initiate arbitrary private conversations.
    try:
        from bot.handlers.join_requests import consume_join_request_dm
        dm_user = await db_session.get(User, user.id)
        if dm_user and dm_user.dm_started and consume_join_request_dm(chat.id, user.id):
            private_text = (
                f"👋 Hello {escape(user.first_name or 'there')}!\n\n"
                f"Welcome to <b>{escape(chat.title or 'the group')}</b>! 🎉\n"
                "I’m ALEXA, the group’s assistant. I can help with group safety, "
                "moderation and fun features.\n\n"
                "Have a great time in the community! 💜"
            )
            await bot.send_message(user.id, private_text, parse_mode="HTML")
            logger.info("Private join-request welcome sent", chat_id=chat.id, user_id=user.id)
    except Exception as exc:
        # A private-message failure must never block the group welcome or join flow.
        logger.debug("Private join welcome unavailable", chat_id=chat.id, user_id=user.id, error=str(exc))


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

    # Group admins and creators are exempt from ChannelGuard.
    joining_user_is_admin = False
    joining_role_verified = False
    try:
        member_status = await event.bot.get_chat_member(chat.id, user.id)
        status_value = str(getattr(member_status.status, "value", member_status.status))
        joining_role_verified = True
        joining_user_is_admin = status_value in ("administrator", "creator", "owner")
    except Exception as exc:
        logger.debug(
            "Could not verify joining member role for ChannelGuard exemption",
            chat_id=chat.id,
            user_id=user.id,
            error=str(exc),
        )

    # ChannelGuard checks only actual URLs in exposed profile fields.
    # A connected channel alone is not a violation.
    bio_setting = await db_session.scalar(
        select(BioLinkProtectionSetting).where(BioLinkProtectionSetting.group_id == chat.id)
    )
    if bio_setting and bio_setting.enabled and joining_role_verified and not joining_user_is_admin:
        member_row = await db_session.scalar(
            select(GroupMember).where(
                GroupMember.group_id == chat.id,
                GroupMember.user_id == user.id,
            )
        )
        if not (member_row and member_row.is_whitelisted):
            try:
                evidence, profile_available = await _channelguard_profile_links(event.bot, user.id)
                if evidence:
                    state, count = await _channelguard_warn_and_mute(
                        event.bot, db_session, chat.id, user, user.full_name or user.first_name or "User"
                    )
                    notice, keyboard_builder = _channelguard_notice(
                        user.id, user.full_name or user.first_name or "User", state, count
                    )
                    if notice:
                        markup = keyboard_builder(chat.id, user.id) if keyboard_builder else None
                        await event.bot.send_message(chat.id, notice, parse_mode="HTML", reply_markup=markup)
                    if state in ("muted", "mute_failed"):
                        return
                elif not profile_available:
                    logger.debug("ChannelGuard profile fields unavailable on join", chat_id=chat.id, user_id=user.id)
            except Exception as exc:
                logger.debug("ChannelGuard join profile check unavailable", chat_id=chat.id, user_id=user.id, error=str(exc))

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
    challenge, created = await create_captcha_challenge(
        db_session, chat.id, user.id, captcha_type, security.captcha_timeout or 60
    )
    if not created:
        # A concurrent join update already created the active challenge.
        return
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
            builder.button(text=str(opt), callback_data=f"captcha_verify:{chat.id}:{user.id}:{challenge.id}:{opt}")
        builder.adjust(2)
        try:
            msg = await event.bot.send_message(chat.id, text, parse_mode="HTML", reply_markup=builder.as_markup())
            challenge.message_id = msg.message_id
        except Exception as e:
            logger.error("Captcha message failed", error=str(e))
    else:
        builder = InlineKeyboardBuilder()
        builder.button(text=_("btn_verify"), callback_data=f"captcha_verify:{chat.id}:{user.id}:{challenge.id}:button")
        try:
            name = mention_user(user.id, safe_username(user))
            text = f"{name}, {_('verification_required')}"
            msg = await event.bot.send_message(chat.id, text, parse_mode="HTML", reply_markup=builder.as_markup())
            challenge.message_id = msg.message_id
        except Exception as e:
            logger.error("Captcha message failed", error=str(e))


def _bio_admin_action_keyboard(group_id: int, user_id: int, *, include_whitelist: bool = True):
    builder = InlineKeyboardBuilder()
    if include_whitelist:
        builder.button(
            text="✅ Whitelist",
            callback_data=f"bio_admin:whitelist:{group_id}:{user_id}",
        )
    builder.button(
        text="🔓 Unmute",
        callback_data=f"bio_unmute:{group_id}:{user_id}",
    )
    builder.adjust(2 if include_whitelist else 1)
    return builder.as_markup()


@router.callback_query(F.data.startswith("bio_unmute:"))
async def bio_unmute_after_profile_fix(callback: CallbackQuery, db_session: AsyncSession, **kwargs):
    """Unmute only the member whose bio-link mute created this verification button."""
    try:
        _, raw_group_id, raw_user_id = (callback.data or "").split(":", 2)
        group_id, user_id = int(raw_group_id), int(raw_user_id)
    except (ValueError, TypeError):
        await callback.answer("This verification button is invalid.", show_alert=True)
        return

    if not callback.message or callback.message.chat.id != group_id:
        await callback.answer("This button belongs to a different group.", show_alert=True)
        return
    if callback.from_user.id != user_id:
        await callback.answer("Only the muted member can use this unmute button.", show_alert=True)
        return

    member = await db_session.scalar(
        select(GroupMember).where(
            GroupMember.group_id == group_id,
            GroupMember.user_id == user_id,
        )
    )
    if not member or not member.is_muted:
        await callback.answer("You are not currently muted by this protection.", show_alert=True)
        return

    # Verify that the latest mute for this member was specifically caused by bio-link protection.
    latest_mute = await db_session.scalar(
        select(ActionLog)
        .where(
            ActionLog.group_id == group_id,
            ActionLog.user_id == user_id,
            ActionLog.action == "mute",
        )
        .order_by(ActionLog.created_at.desc(), ActionLog.id.desc())
        .limit(1)
    )
    details = (latest_mute.details or {}) if latest_mute else {}
    reason = details.get("reason", "") if isinstance(details, dict) else ""
    if reason not in {
        "Link found in profile bio (Bio Link Protection)",
        "Bio-link warning limit reached",
    }:
        await callback.answer("This mute was not caused by Bio Link Protection.", show_alert=True)
        return

    if not member.is_whitelisted:
        try:
            evidence, profile_available = await _channelguard_profile_links(callback.bot, user_id)
            if not profile_available:
                await callback.answer("Telegram did not expose your bio/connected-channel details for verification. Try again later.", show_alert=True)
                return
            if evidence:
                await callback.answer("A link is still in your bio or connected channel. Remove it first, or ask an admin to whitelist you.", show_alert=True)
                return
        except Exception as exc:
            logger.warning("Could not verify profile before ChannelGuard unmute", chat_id=group_id, user_id=user_id, error=str(exc))
            await callback.answer("Could not verify your profile right now. Please try again later.", show_alert=True)
            return

    ok = await unmute_user(callback.bot, db_session, group_id, user_id, callback.bot.id)
    if not ok:
        await callback.answer("Unmute failed. Please ask a group admin to check ALEXA's permissions.", show_alert=True)
        return
    try:
        await db_session.commit()
    except Exception as exc:
        await db_session.rollback()
        logger.warning("Could not save bio-link unmute", chat_id=group_id, user_id=user_id, error=str(exc))
        await callback.answer("Unmute could not be saved. Please try again.", show_alert=True)
        return

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.answer("Bio verified — you have been unmuted.")
    try:
        await callback.bot.send_message(
            group_id,
            f"✅ <a href=\"tg://user?id={user_id}\">Member</a> removed the bio link and has been unmuted.",
            parse_mode="HTML",
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("bio_admin:"))
async def bio_link_admin_action(callback: CallbackQuery, db_session: AsyncSession, **kwargs):
    """Allow group admins to whitelist a bio-link member or unmute and clear warnings."""
    try:
        _, action, raw_group_id, raw_user_id = (callback.data or "").split(":", 3)
        group_id, user_id = int(raw_group_id), int(raw_user_id)
    except (ValueError, TypeError):
        await callback.answer("This Bio Link Protection action is invalid.", show_alert=True)
        return

    if action not in {"whitelist", "unmute"}:
        await callback.answer("Unknown Bio Link Protection action.", show_alert=True)
        return
    if not callback.message or callback.message.chat.id != group_id:
        await callback.answer("This button belongs to a different group.", show_alert=True)
        return

    try:
        admin = await callback.bot.get_chat_member(group_id, callback.from_user.id)
    except Exception:
        await callback.answer("Could not verify your group admin status.", show_alert=True)
        return
    if admin.status not in {"administrator", "creator", "owner"}:
        await callback.answer("Only group admins can use these buttons.", show_alert=True)
        return
    # The bot performs the Telegram unmute; the clicking admin only needs
    # to be a group admin. Check the bot's own Restrict Members permission
    # instead of incorrectly requiring that permission from the human admin.
    if action == "unmute":
        try:
            bot_member = await callback.bot.get_chat_member(group_id, callback.bot.id)
            bot_status = str(getattr(bot_member.status, "value", bot_member.status))
            bot_can_restrict = (
                bot_status in {"creator", "owner"}
                or bool(getattr(bot_member, "can_restrict_members", False))
            )
        except Exception:
            bot_can_restrict = False
        if not bot_can_restrict:
            await callback.answer("Alexa needs the Restrict Members permission to unmute this member.", show_alert=True)
            return

    member = await db_session.scalar(
        select(GroupMember).where(
            GroupMember.group_id == group_id,
            GroupMember.user_id == user_id,
        )
    )
    if member is None:
        await callback.answer("The member record is unavailable. No changes were made.", show_alert=True)
        return

    latest_mute = await db_session.scalar(
        select(ActionLog)
        .where(
            ActionLog.group_id == group_id,
            ActionLog.user_id == user_id,
            ActionLog.action == "mute",
        )
        .order_by(ActionLog.created_at.desc(), ActionLog.id.desc())
        .limit(1)
    )
    details = (latest_mute.details or {}) if latest_mute else {}
    mute_reason = details.get("reason", "") if isinstance(details, dict) else ""
    if not latest_mute or not ("bio" in str(mute_reason).lower() or "link warnings reached" in str(mute_reason).lower()):
        await callback.answer("This member's latest mute was not caused by Bio Link Protection.", show_alert=True)
        return

    if action == "unmute" and not member.is_muted:
        await callback.answer("This member is not currently muted.", show_alert=True)
        return

    if action == "whitelist":
        try:
            member.is_whitelisted = True
            await db_session.commit()
        except Exception as exc:
            await db_session.rollback()
            logger.warning("Could not whitelist bio-link member", group_id=group_id, user_id=user_id, error=str(exc))
            await callback.answer("Could not save the whitelist entry. Try again.", show_alert=True)
            return

        try:
            await callback.message.edit_reply_markup(
                reply_markup=_bio_admin_action_keyboard(group_id, user_id, include_whitelist=False)
            )
        except Exception:
            pass
        await callback.answer("Member added to the Bio Link Protection whitelist.")
        return

    ok = await unmute_user(callback.bot, db_session, group_id, user_id, callback.from_user.id)
    if not ok:
        await db_session.rollback()
        await callback.answer("Unmute failed. Check Alexa's Restrict Members permission.", show_alert=True)
        return
    try:
        # Unmute must never erase the member's warning history.
        await db_session.commit()
    except Exception as exc:
        await db_session.rollback()
        logger.warning("Could not save ChannelGuard unmute", group_id=group_id, user_id=user_id, error=str(exc))
        await callback.answer("Unmute could not be saved. Please try again.", show_alert=True)
        return

    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    try:
        await callback.bot.send_message(
            group_id,
            f"✅ {mention_user(user_id, f'User {user_id}')} has been unmuted by an admin. Existing warnings were kept.",
            parse_mode="HTML",
        )
    except Exception:
        pass
    await callback.answer("Member unmuted. Warning history was kept.")


@router.callback_query(F.data.startswith("captcha_verify:"))
async def captcha_verify(callback, db_session: AsyncSession, **kwargs):
    parts = callback.data.split(":")
    if len(parts) != 5:
        await callback.answer("This captcha button is outdated. Please use the latest challenge.", show_alert=True)
        return
    try:
        group_id = int(parts[1])
        user_id = int(parts[2])
        challenge_id = int(parts[3])
        answer = parts[4]
    except (TypeError, ValueError):
        await callback.answer("This captcha button is invalid.", show_alert=True)
        return

    if callback.from_user.id != user_id:
        await callback.answer("This captcha is not for you!", show_alert=True)
        return

    from bot.services.security_service import verify_captcha
    success = await verify_captcha(db_session, group_id, user_id, challenge_id, answer)

    group = await db_session.get(Group, group_id)
    lang = group.language if group else "en"
    _ = lambda key, **kw: get_text(lang, key, **kw)

    if success is None:
        # Expired, superseded, or already-consumed buttons are harmless and must
        # never trigger the wrong-answer punishment path.
        await callback.answer("This captcha has expired or was already used. Please use the latest challenge.", show_alert=True)
        return

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

    # Do not blanket-exempt Telegram automatic channel forwards. When the
    # group's Anti-Forward, Lock Forwards, Anti-Link, or Anti-NSFW controls are
    # enabled, those channel-originated messages must pass through the same
    # configured checks as other incoming content.

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
        try:
            group_security, _created = await _insert_or_get(
                db_session, SecuritySettings(group_id=message.chat.id),
                SecuritySettings, SecuritySettings.group_id == message.chat.id,
            )
        except Exception:
            await db_session.rollback()
            logger.exception("Could not initialize group security settings", group_id=message.chat.id)
            return
    # Never moderate ALEXA's own outbound messages. This guard must run
    # BEFORE emergency lockdown and link/content locks, otherwise the bot can
    # delete its own links/promotions when those protections are enabled.
    if message.from_user and message.from_user.is_bot and message.from_user.id == message.bot.id:
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
                db_user, _created = await _insert_or_get(
                    db_session,
                    User(
                        id=message.from_user.id,
                        telegram_id=message.from_user.id,
                        first_name=message.from_user.first_name or "User",
                        last_name=message.from_user.last_name,
                        username=message.from_user.username,
                        is_bot=False,
                    ),
                    User, User.id == message.from_user.id,
                )
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

    # Resolve admin status once for link exemptions. Admin links are exempt
    # automatically (no command/configuration); non-admin behavior is unchanged.
    sender_is_admin = False
    sender_role_verified = False
    if message.from_user:
        try:
            member_status = await message.bot.get_chat_member(
                message.chat.id, message.from_user.id
            )
            sender_status = str(getattr(member_status.status, "value", member_status.status))
            sender_role_verified = True
            sender_is_admin = sender_status in ("administrator", "creator", "owner")
        except Exception as exc:
            logger.debug(
                "Could not verify sender role for automatic admin link exemption",
                chat_id=message.chat.id,
                user_id=message.from_user.id,
                error=str(exc),
            )

    # Enforce each content lock independently before other moderation branches.
    # Admin messages containing links bypass only text/link locks; other content
    # locks (photos, videos, etc.) still apply as usual.
    if message.from_user:
        content = message.text or message.caption or ""
        admin_link_message = sender_is_admin and contains_link(content)
        locked_reason = None
        if group_security.lock_text and message.text and not message.caption and not admin_link_message:
            locked_reason = "text"
        elif group_security.lock_links and contains_link(content) and not sender_is_admin:
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

    # ChannelGuard checks profile fields only when its own per-group switch is ON.
    # Ordinary message links remain governed by the group's existing Anti-Link rules.
    if message.from_user and not message.from_user.is_bot:
        bio_setting = await db_session.scalar(
            select(BioLinkProtectionSetting).where(
                BioLinkProtectionSetting.group_id == db_group.id
            )
        )
        if bio_setting and bio_setting.enabled and sender_role_verified and not sender_is_admin:
            member_row = await db_session.scalar(
                select(GroupMember).where(
                    GroupMember.group_id == db_group.id,
                    GroupMember.user_id == message.from_user.id,
                )
            )
            if not (member_row and member_row.is_whitelisted):
                try:
                    evidence, profile_available = await _channelguard_profile_links(
                        message.bot, message.from_user.id
                    )
                    if evidence:
                        state, count = await _channelguard_warn_and_mute(
                            message.bot,
                            db_session,
                            db_group.id,
                            message.from_user,
                            message.from_user.full_name or message.from_user.first_name or "User",
                        )
                        notice, keyboard_builder = _channelguard_notice(
                            message.from_user.id,
                            message.from_user.full_name or message.from_user.first_name or "User",
                            state,
                            count,
                        )
                        if notice:
                            markup = keyboard_builder(db_group.id, message.from_user.id) if keyboard_builder else None
                            await message.bot.send_message(
                                message.chat.id, notice, parse_mode="HTML", reply_markup=markup
                            )
                        if state in ("muted", "mute_failed"):
                            return
                    elif not profile_available:
                        logger.debug(
                            "ChannelGuard profile fields unavailable for message sender",
                            chat_id=message.chat.id,
                            user_id=message.from_user.id,
                        )
                except Exception as exc:
                    logger.debug(
                        "ChannelGuard profile scan unavailable",
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

    # Sticker approval remains valid even when Lock All is enabled; only the
    # sticker lock is bypassed for this member. All other locks stay enforced.
    violations = await check_message_violations(message, group_security, approved_sticker_user=approved_sticker_user)
    # Automatically inspect sticker artwork while Anti-NSFW is enabled.
    # Admins retain the existing exemption; API failures never trigger deletion.
    if group_security.anti_nsfw and message.sticker and not sender_is_admin:
        try:
            if await is_nsfw_sticker(message.bot, message.sticker):
                violations.append("nsfw_sticker")
        except Exception:
            # Keep a media-scan failure from skipping the remaining moderation
            # checks for this message.
            logger.exception(
                "Anti-NSFW sticker scan crashed; remaining moderation checks continue",
                chat_id=message.chat.id,
                message_id=message.message_id,
            )

    # Admins are exempt from NSFW/abuse screening, but all unrelated
    # moderation rules continue to apply to their messages.
    if sender_is_admin:
        violations = [v for v in violations if v not in {"nsfw", "abuse", "nsfw_sticker"}]
    # Group admins/creator are automatically exempt from link moderation,
    # including invite URLs and link-lock violations. When their message is a
    # link, do not let the generic text lock re-delete it; all other violations
    # (spam, scam, photos, etc.) remain active.
    if message.from_user and sender_is_admin and contains_link(message.text or message.caption or ""):
        admin_link_violations = {"link", "invite_link", "locked_link"}
        if message.text and not message.caption:
            admin_link_violations.add("locked_text")
        violations = [v for v in violations if v not in admin_link_violations]

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
        if "nsfw_sticker" in violations or any(reason.startswith("locked_") for reason in violations):
            action = "delete"
        elif not db_user:
            # Without a persisted user record, safely delete the violating message
            # rather than attempting warn/mute/ban actions that require a DB user.
            action = "delete"
        else:
            if "flood" in violations:
                action = group_security.flood_action or "delete"
            elif any(reason in violations for reason in ("link", "invite_link")):
                action = group_security.link_action or "delete"
            else:
                action = group_security.spam_action or "delete"
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
        await db_session.execute(
            update(GroupMember)
            .where(GroupMember.id == member.id)
            .values(
                message_count=func.coalesce(GroupMember.message_count, 0) + 1,
                last_activity=datetime.now(timezone.utc),
            )
            .execution_options(synchronize_session=False)
        )

    # Track per-group daily totals using India-local calendar dates so the
    # owner AI activity report reflects actual messages received today.
    from zoneinfo import ZoneInfo
    day_key = datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()
    daily_stats = await db_session.scalar(
        select(DailyGroupMessageStat).where(
            DailyGroupMessageStat.group_id == db_group.id,
            DailyGroupMessageStat.day_key == day_key,
        )
    )
    if daily_stats is None:
        daily_stats, created = await _insert_or_get(
            db_session,
            DailyGroupMessageStat(group_id=db_group.id, day_key=day_key, message_count=1),
            DailyGroupMessageStat,
            DailyGroupMessageStat.group_id == db_group.id,
            DailyGroupMessageStat.day_key == day_key,
        )
        if not created:
            await db_session.execute(
                update(DailyGroupMessageStat)
                .where(DailyGroupMessageStat.id == daily_stats.id)
                .values(message_count=func.coalesce(DailyGroupMessageStat.message_count, 0) + 1)
                .execution_options(synchronize_session=False)
            )
    else:
        await db_session.execute(
            update(DailyGroupMessageStat)
            .where(DailyGroupMessageStat.id == daily_stats.id)
            .values(message_count=func.coalesce(DailyGroupMessageStat.message_count, 0) + 1)
            .execution_options(synchronize_session=False)
        )

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
        gs_stats, created = await _insert_or_get(
            db_session, GroupStats(group_id=db_group.id, total_messages=1),
            GroupStats, GroupStats.group_id == db_group.id,
        )
        if not created:
            await db_session.execute(
                update(GroupStats)
                .where(GroupStats.id == gs_stats.id)
                .values(total_messages=func.coalesce(GroupStats.total_messages, 0) + 1)
                .execution_options(synchronize_session=False)
            )
    else:
        await db_session.execute(
            update(GroupStats)
            .where(GroupStats.id == gs_stats.id)
            .values(total_messages=func.coalesce(GroupStats.total_messages, 0) + 1)
            .execution_options(synchronize_session=False)
        )