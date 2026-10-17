from aiogram import Router, F, Bot
from aiogram.types import CallbackQuery, Message
from aiogram.filters import Command
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, delete, func
from bot.database.models import User, GroupMember, GroupSettings, Warning, SecuritySettings, BioLinkProtectionSetting, BioLinkProtectionConfig
from bot.keyboards.moderation import moderation_menu_kb, bio_link_protection_kb, bio_link_action_kb, mute_duration_kb, warn_actions_kb
from bot.keyboards.main_menu import back_button_kb
from bot.services.moderation_service import (
    ban_user, unban_user, kick_user, mute_user, unmute_user,
    warn_user, unwarn_user, get_user_warnings, get_user_history, add_note, get_notes
)
from bot.utils.helpers import format_duration, mention_user, safe_username
import structlog
from html import escape
from bot.middlewares.admin_actions import AdminActionMiddleware

logger = structlog.get_logger()
router = Router()

_bio_link_settings_drafts = {}
router.message.outer_middleware(AdminActionMiddleware(
    commands={"ban", "unban", "kick", "mute", "unmute", "warn", "unwarn", "warns", "warnings", "history", "note", "del", "purg", "pin", "unpin", "ro", "setrole", "clearrole", "role", "resetwarns", "promote", "demote"}
))
router.callback_query.outer_middleware(AdminActionMiddleware(prefixes=("mod:", "mute_duration:", "ban_duration:", "warn_action:", "ban_confirm:")))


async def get_target_user(message: Message, session: AsyncSession) -> tuple:
    target = None
    reason = ""
    if message.reply_to_message:
        tg_user = message.reply_to_message.from_user
        target = await session.get(User, tg_user.id)
        if not target:
            target = User(
                id=tg_user.id, telegram_id=tg_user.id, first_name=tg_user.first_name or "User",
                last_name=tg_user.last_name, username=tg_user.username, is_bot=tg_user.is_bot,
            )
            session.add(target)
            await session.flush()
        args = message.text.split(None, 1)[1] if len(message.text.split()) > 1 else ""
        reason = args
    elif len(message.text.split()) > 1:
        parts = message.text.split(None, 2)
        identifier = parts[1]
        reason = parts[2] if len(parts) > 2 else ""
        if identifier.startswith("@"):
            target = await session.scalar(select(User).where(func.lower(User.username) == identifier[1:].lower()))
        else:
            try:
                uid = int(identifier)
                target = await session.get(User, uid)
                if target is None and uid > 0:
                    target = User(id=uid, telegram_id=uid, first_name=f"User {uid}", is_bot=False)
                    session.add(target)
                    await session.flush()
            except ValueError:
                target = await session.scalar(select(User).where(func.lower(User.username) == identifier.lower()))
    return target, reason


async def _bio_link_protection_enabled(db_session: AsyncSession, chat_id: int) -> bool:
    setting = await db_session.scalar(
        select(BioLinkProtectionSetting).where(BioLinkProtectionSetting.group_id == chat_id)
    )
    if setting is not None:
        return bool(setting.enabled)
    # Keep the previous behavior until an admin makes an explicit choice.
    security = await db_session.scalar(
        select(SecuritySettings).where(SecuritySettings.group_id == chat_id)
    )
    return bool(security and security.anti_link)


@router.callback_query(F.data == "menu:moderation")
async def moderation_menu(callback: CallbackQuery, _: callable, db_session: AsyncSession, **kwargs):
    enabled = await _bio_link_protection_enabled(db_session, callback.message.chat.id)
    await callback.message.edit_text(
        _("moderation_menu"),
        reply_markup=moderation_menu_kb(_, bio_link_enabled=enabled),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "mod:menu")
async def moderation_menu_back(callback: CallbackQuery, _: callable, db_session: AsyncSession, **kwargs):
    enabled = await _bio_link_protection_enabled(db_session, callback.message.chat.id)
    await callback.message.edit_text(
        _("moderation_menu"),
        reply_markup=moderation_menu_kb(_, bio_link_enabled=enabled),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "mod:bio_link_protection")
async def open_bio_link_protection(callback: CallbackQuery, db_session: AsyncSession, **kwargs):
    chat = callback.message.chat
    if chat.type not in ("group", "supergroup"):
        await callback.answer("Open Moderation inside the group you want to configure.", show_alert=True)
        return
    try:
        actor = await callback.bot.get_chat_member(chat.id, callback.from_user.id)
        if actor.status not in ("administrator", "creator"):
            await callback.answer("Only group admins can change this protection.", show_alert=True)
            return
    except Exception:
        await callback.answer("Could not verify your group-admin permissions.", show_alert=True)
        return
    enabled = await _bio_link_protection_enabled(db_session, chat.id)
    await callback.message.edit_text(
        "🛡️ <b>Bio Link Protection</b>\n\n"
        "When enabled, ALEXA checks for links in member bios where Telegram makes the bio available, "
        "and applies the group's configured protection.\n\n"
        f"Current status: <b>{'🟢 ON' if enabled else '🔴 OFF'}</b>",
        parse_mode="HTML",
        reply_markup=bio_link_protection_kb(enabled),
    )
    await callback.answer()


@router.callback_query(F.data == "mod:bio_link_settings")
async def open_bio_link_settings(callback: CallbackQuery, db_session: AsyncSession, **kwargs):
    chat = callback.message.chat
    if chat.type not in ("group", "supergroup"):
        await callback.answer("Open this menu inside the group you want to configure.", show_alert=True)
        return
    try:
        actor = await callback.bot.get_chat_member(chat.id, callback.from_user.id)
        if actor.status not in ("administrator", "creator"):
            await callback.answer("Only group admins can change these settings.", show_alert=True)
            return
    except Exception:
        await callback.answer("Could not verify your group-admin permissions.", show_alert=True)
        return
    config = await db_session.scalar(select(BioLinkProtectionConfig).where(BioLinkProtectionConfig.group_id == chat.id))
    action = config.action if config else "warn_mute"
    duration = config.mute_duration if config else 3600
    _bio_link_settings_drafts[(chat.id, callback.from_user.id)] = {"action": action, "duration": duration}
    await callback.message.edit_text(
        "⚙️ <b>Bio Link Protection Settings</b>\n\n"
        "Choose what ALEXA should do when a member's bio contains a link. "
        "Select an action and, if using mute, a duration. Then press Save Settings.\n\n"
        f"Selected action: <b>{action.upper()}</b>\n"
        f"Mute duration: <b>{duration // 60} minutes</b>",
        parse_mode="HTML",
        reply_markup=bio_link_action_kb(action, duration),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("mod:bio_action:"))
async def select_bio_link_action(callback: CallbackQuery, **kwargs):
    action = callback.data.rsplit(":", 1)[-1]
    if action not in ("warn_mute", "warn_ban", "mute", "ban"):
        await callback.answer("Invalid action.", show_alert=True)
        return
    key = (callback.message.chat.id, callback.from_user.id)
    draft = _bio_link_settings_drafts.setdefault(key, {"action": "warn_mute", "duration": 3600})
    draft["action"] = action
    await callback.message.edit_reply_markup(reply_markup=bio_link_action_kb(draft["action"], draft["duration"]))
    await callback.answer(f"Action selected: {action.upper()}. Save to apply.")


@router.callback_query(F.data.startswith("mod:bio_duration:"))
async def select_bio_link_duration(callback: CallbackQuery, **kwargs):
    try:
        duration = int(callback.data.rsplit(":", 1)[-1])
    except ValueError:
        await callback.answer("Invalid duration.", show_alert=True)
        return
    if duration not in (900, 1800, 3600, 21600, 43200, 86400):
        await callback.answer("Invalid duration.", show_alert=True)
        return
    key = (callback.message.chat.id, callback.from_user.id)
    draft = _bio_link_settings_drafts.setdefault(key, {"action": "warn_mute", "duration": 3600})
    draft["duration"] = duration
    await callback.message.edit_reply_markup(reply_markup=bio_link_action_kb(draft["action"], duration))
    await callback.answer("Mute duration selected. Save to apply.")


@router.callback_query(F.data == "mod:bio_settings_save")
async def save_bio_link_settings(callback: CallbackQuery, db_session: AsyncSession, **kwargs):
    chat = callback.message.chat
    if chat.type not in ("group", "supergroup"):
        await callback.answer("Open this menu inside the group you want to configure.", show_alert=True)
        return
    try:
        actor = await callback.bot.get_chat_member(chat.id, callback.from_user.id)
        if actor.status not in ("administrator", "creator"):
            await callback.answer("Only group admins can save these settings.", show_alert=True)
            return
    except Exception:
        await callback.answer("Could not verify your group-admin permissions.", show_alert=True)
        return
    key = (chat.id, callback.from_user.id)
    draft = _bio_link_settings_drafts.get(key, {"action": "warn_mute", "duration": 3600})
    config = await db_session.scalar(select(BioLinkProtectionConfig).where(BioLinkProtectionConfig.group_id == chat.id))
    if config is None:
        config = BioLinkProtectionConfig(group_id=chat.id, action=draft["action"], mute_duration=draft["duration"])
        db_session.add(config)
    else:
        config.action = draft["action"]
        config.mute_duration = draft["duration"]
    try:
        await db_session.commit()
    except Exception as exc:
        await db_session.rollback()
        logger.warning("Failed to save bio-link protection configuration", group_id=chat.id, error=str(exc))
        await callback.answer("Could not save settings. Try again.", show_alert=True)
        return
    _bio_link_settings_drafts.pop(key, None)
    await callback.message.edit_text(
        "✅ <b>Bio Link Protection settings saved</b>\n\n"
        f"Action: <b>{config.action.upper()}</b>\n"
        f"Mute duration: <b>{config.mute_duration // 60} minutes</b>\n\n"
        "These settings apply only to this group.",
        parse_mode="HTML",
        reply_markup=bio_link_protection_kb(await _bio_link_protection_enabled(db_session, chat.id)),
    )
    await callback.answer("Settings saved.")


@router.callback_query(F.data == "mod:bio_link_status")
async def bio_link_status(callback: CallbackQuery, db_session: AsyncSession, **kwargs):
    enabled = await _bio_link_protection_enabled(db_session, callback.message.chat.id)
    await callback.answer("Bio Link Protection is " + ("ON." if enabled else "OFF."), show_alert=True)


async def _set_bio_link_protection(callback: CallbackQuery, db_session: AsyncSession, enabled: bool):
    chat = callback.message.chat
    if chat.type not in ("group", "supergroup"):
        await callback.answer("Open Moderation inside the group you want to configure.", show_alert=True)
        return
    try:
        actor = await callback.bot.get_chat_member(chat.id, callback.from_user.id)
        if actor.status not in ("administrator", "creator"):
            await callback.answer("Only group admins can change this protection.", show_alert=True)
            return
    except Exception:
        await callback.answer("Could not verify your group-admin permissions.", show_alert=True)
        return

    setting = await db_session.scalar(
        select(BioLinkProtectionSetting).where(BioLinkProtectionSetting.group_id == chat.id)
    )
    if setting is None:
        setting = BioLinkProtectionSetting(group_id=chat.id, enabled=enabled)
        db_session.add(setting)
    else:
        setting.enabled = enabled
    try:
        await db_session.commit()
    except Exception as exc:
        await db_session.rollback()
        logger.warning("Failed to save bio-link protection setting", group_id=chat.id, error=str(exc))
        await callback.answer("Could not save this setting. Try again.", show_alert=True)
        return

    await callback.message.edit_text(
        "🛡️ <b>Bio Link Protection</b>\n\n"
        "When enabled, ALEXA checks for links in member bios where Telegram makes the bio available, "
        "and applies the group's configured protection.\n\n"
        f"Current status: <b>{'🟢 ON' if enabled else '🔴 OFF'}</b>",
        parse_mode="HTML",
        reply_markup=bio_link_protection_kb(enabled),
    )
    await callback.answer("Bio Link Protection " + ("enabled." if enabled else "disabled."))


@router.callback_query(F.data == "mod:bio_link_on")
async def enable_bio_link_protection(callback: CallbackQuery, db_session: AsyncSession, **kwargs):
    await _set_bio_link_protection(callback, db_session, True)


@router.callback_query(F.data == "mod:bio_link_off")
async def disable_bio_link_protection(callback: CallbackQuery, db_session: AsyncSession, **kwargs):
    await _set_bio_link_protection(callback, db_session, False)


@router.message(Command("ban"))
async def cmd_ban(message: Message, bot: Bot, _: callable, db_session: AsyncSession, db_group=None, db_user=None, **kwargs):
    if message.chat.type == "private":
        await message.reply(_("error_group_only"))
        return
    target, reason = await get_target_user(message, db_session)
    if not target:
        await message.reply(_("provide_user"))
        return
    success = await ban_user(bot, db_session, message.chat.id, target.id, message.from_user.id, reason)
    if success:
        name = mention_user(target.id, safe_username(target))
        await message.reply(_("user_banned").format(name=name, reason=reason or _("no_reason")), parse_mode="HTML")


@router.message(Command("unban"))
async def cmd_unban(message: Message, bot: Bot, _: callable, db_session: AsyncSession, **kwargs):
    if message.chat.type == "private":
        return
    target, _ = await get_target_user(message, db_session)
    if not target:
        await message.reply(_("provide_user"))
        return
    success = await unban_user(bot, db_session, message.chat.id, target.id, message.from_user.id)
    if success:
        await message.reply(_("user_unbanned").format(name=safe_username(target)))


@router.message(Command("kick"))
async def cmd_kick(message: Message, bot: Bot, _: callable, db_session: AsyncSession, **kwargs):
    if message.chat.type == "private":
        return
    target, reason = await get_target_user(message, db_session)
    if not target:
        await message.reply(_("provide_user"))
        return
    success = await kick_user(bot, db_session, message.chat.id, target.id, message.from_user.id, reason)
    if success:
        name = mention_user(target.id, safe_username(target))
        await message.reply(_("user_kicked").format(name=name, reason=reason or _("no_reason")), parse_mode="HTML")


@router.message(Command("mute"))
async def cmd_mute(message: Message, bot: Bot, _: callable, db_session: AsyncSession, **kwargs):
    if message.chat.type == "private":
        return
    target, args = await get_target_user(message, db_session)
    if not target:
        await message.reply(_("provide_user"))
        return

    from bot.utils.helpers import parse_duration
    duration = None
    reason = args
    if args:
        parts = args.split(None, 1)
        dur = parse_duration(parts[0])
        if dur:
            duration = dur
            reason = parts[1] if len(parts) > 1 else ""

    success = await mute_user(bot, db_session, message.chat.id, target.id, message.from_user.id, duration, reason)
    if success:
        name = mention_user(target.id, safe_username(target))
        dur_str = format_duration(duration) if duration else "Forever"
        await message.reply(
            _("user_muted").format(name=name, duration=dur_str, reason=reason or _("no_reason")),
            parse_mode="HTML",
        )
    else:
        await message.reply("❌ I couldn't mute this user. Check that ALEXA is an admin with Restrict Members permission and that the target is not a group admin.")


@router.message(Command("unmute"))
async def cmd_unmute(message: Message, bot: Bot, _: callable, db_session: AsyncSession, **kwargs):
    if message.chat.type == "private":
        await message.answer("This command only works in groups.")
        return
    target, _reason = await get_target_user(message, db_session)
    if not target:
        await message.answer(
            "Reply to the muted member's message with /unmute, or use /unmute <user ID>."
        )
        return
    success = await unmute_user(bot, db_session, message.chat.id, target.id, message.from_user.id)
    if success:
        # Use a literal response so a missing translation key cannot suppress feedback.
        response = f"✅ {escape(safe_username(target))} has been unmuted."
    else:
        response = (
            "❌ I couldn't unmute this user. Check that ALEXA has Restrict Members permission "
            "and that the target is still in the group."
        )
    try:
        await message.answer(response, parse_mode="HTML")
    except Exception as exc:
        logger.warning("Could not send /unmute result", chat_id=message.chat.id, user_id=target.id, error=str(exc))


@router.message(Command("warn"))
async def cmd_warn(message: Message, bot: Bot, _: callable, db_session: AsyncSession, db_group=None, **kwargs):
    if message.chat.type == "private":
        return
    target, reason = await get_target_user(message, db_session)
    if not target:
        await message.reply(_("provide_user"))
        return

    gs = await db_session.scalar(select(GroupSettings).where(GroupSettings.group_id == message.chat.id))
    max_warns = gs.max_warns if gs else 3
    warn_action = gs.warn_action if gs else "mute"

    count, reached_max = await warn_user(db_session, message.chat.id, target.id, message.from_user.id, reason, max_warns)
    name = mention_user(target.id, safe_username(target))
    await message.reply(
        _("user_warned").format(name=name, count=count, max=max_warns, reason=reason or _("no_reason")),
        parse_mode="HTML",
    )

    if reached_max:
        await message.reply(
            _("max_warns_reached").format(name=safe_username(target), action=warn_action),
            parse_mode="HTML",
        )
        if warn_action == "ban":
            await ban_user(bot, db_session, message.chat.id, target.id, message.from_user.id, "Max warnings reached")
        elif warn_action == "kick":
            await kick_user(bot, db_session, message.chat.id, target.id, message.from_user.id, "Max warnings reached")
        elif warn_action == "mute":
            await mute_user(bot, db_session, message.chat.id, target.id, message.from_user.id, 3600, "Max warnings reached")


@router.message(Command("unwarn"))
async def cmd_unwarn(message: Message, _: callable, db_session: AsyncSession, **kwargs):
    if message.chat.type == "private":
        return
    target, _ = await get_target_user(message, db_session)
    if not target:
        await message.reply(_("provide_user"))
        return
    success = await unwarn_user(db_session, message.chat.id, target.id)
    if success:
        await message.reply(_("user_unwarned").format(name=safe_username(target)))


@router.message(Command("warns", "warnings"))
async def cmd_warns(message: Message, _: callable, db_session: AsyncSession, **kwargs):
    if message.chat.type == "private":
        return
    target, _ = await get_target_user(message, db_session)
    if not target:
        if message.reply_to_message:
            target_id = message.reply_to_message.from_user.id
        else:
            target_id = message.from_user.id
        target = await db_session.get(User, target_id)

    if not target:
        return
    warnings = await get_user_warnings(db_session, message.chat.id, target.id)
    text = f"⚠️ <b>Warnings for {safe_username(target)}</b>\n\n"
    if not warnings:
        text += "No warnings."
    else:
        for i, w in enumerate(warnings, 1):
            text += f"{i}. {w.reason or 'No reason'} — {w.created_at.strftime('%Y-%m-%d')}\n"
    await message.reply(text, parse_mode="HTML")


@router.message(Command("note"))
async def cmd_note(message: Message, _: callable, db_session: AsyncSession, **kwargs):
    if message.chat.type == "private":
        return
    if not message.reply_to_message:
        await message.reply("Reply to a user's message to add a note.")
        return
    parts = message.text.split(None, 1)
    if len(parts) < 2:
        await message.reply("Usage: /note <text>")
        return
    note_text = parts[1]
    await add_note(db_session, message.chat.id, message.reply_to_message.from_user.id, message.from_user.id, note_text)
    await message.reply(f"📝 Note added for {safe_username(message.reply_to_message.from_user)}")


@router.callback_query(F.data.startswith("mute_duration:"))
async def apply_mute_duration(callback: CallbackQuery, db_session: AsyncSession, **kwargs):
    parts = callback.data.split(":", 2)
    try:
        user_id = int(parts[1]); duration_text = parts[2]
    except (ValueError, IndexError):
        await callback.answer("Invalid mute request.", show_alert=True); return
    from bot.utils.helpers import parse_duration
    from bot.services.moderation_service import mute_user
    seconds = None if duration_text == "forever" else parse_duration(duration_text)
    if duration_text != "forever" and not seconds:
        await callback.answer("Invalid duration.", show_alert=True); return
    ok = await mute_user(callback.bot, db_session, callback.message.chat.id, user_id, callback.from_user.id, seconds, "Selected from moderation menu")
    await callback.answer("Member muted." if ok else "Mute failed; check bot permissions.", show_alert=True)


@router.callback_query(F.data.startswith("ban_duration:"))
async def apply_ban_duration(callback: CallbackQuery, db_session: AsyncSession, **kwargs):
    parts = callback.data.split(":", 2)
    try:
        user_id = int(parts[1]); duration_text = parts[2]
    except (ValueError, IndexError):
        await callback.answer("Invalid ban request.", show_alert=True); return
    from datetime import datetime, timedelta, timezone
    from bot.utils.helpers import parse_duration
    seconds = None if duration_text == "forever" else parse_duration(duration_text)
    if duration_text != "forever" and not seconds:
        await callback.answer("Invalid duration.", show_alert=True); return
    until = datetime.now(timezone.utc) + timedelta(seconds=seconds) if seconds else None
    ok = await ban_user(callback.bot, db_session, callback.message.chat.id, user_id, callback.from_user.id, "Selected from moderation menu", until)
    await callback.answer("Member banned." if ok else "Ban failed; check bot permissions.", show_alert=True)


@router.callback_query(F.data.startswith("warn_action:"))
async def apply_warn_action(callback: CallbackQuery, db_session: AsyncSession, **kwargs):
    parts = callback.data.split(":", 2)
    try:
        action = parts[1]; user_id = int(parts[2])
    except (ValueError, IndexError):
        await callback.answer("Invalid warning action.", show_alert=True); return
    if action == "mute":
        ok = await mute_user(callback.bot, db_session, callback.message.chat.id, user_id, callback.from_user.id, 3600, "Warning action")
    elif action == "kick":
        ok = await kick_user(callback.bot, db_session, callback.message.chat.id, user_id, callback.from_user.id, "Warning action")
    elif action == "ban":
        ok = await ban_user(callback.bot, db_session, callback.message.chat.id, user_id, callback.from_user.id, "Warning action")
    else:
        await callback.answer("Unknown action.", show_alert=True); return
    await callback.answer("Action applied." if ok else "Action failed; check bot permissions.", show_alert=True)


@router.callback_query(F.data.startswith("ban_confirm:"))
async def apply_ban_confirm(callback: CallbackQuery, db_session: AsyncSession, **kwargs):
    try: user_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Invalid user.", show_alert=True); return
    ok = await ban_user(callback.bot, db_session, callback.message.chat.id, user_id, callback.from_user.id, "Confirmed ban")
    await callback.answer("Member banned." if ok else "Ban failed; check bot permissions.", show_alert=True)


@router.callback_query(F.data.in_({"mod:ban", "mod:unban", "mod:kick", "mod:mute", "mod:unmute", "mod:warn", "mod:unwarn", "mod:tban", "mod:tmute"}))
async def cb_mod_command_hint(callback: CallbackQuery, db_session: AsyncSession, **kwargs):
    """Turn moderation menu buttons into a target picker instead of a dead-end hint."""
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from bot.database.models import User
    if callback.message.chat.type not in ("group", "supergroup"):
        await callback.answer("Open this menu in the group you want to moderate.", show_alert=True)
        return
    try:
        actor = await callback.bot.get_chat_member(callback.message.chat.id, callback.from_user.id)
        if actor.status not in ("administrator", "creator"):
            await callback.answer("Only group admins can use moderation actions.", show_alert=True)
            return
    except Exception:
        await callback.answer("Could not verify your admin permissions.", show_alert=True)
        return

    action = callback.data.split(":", 1)[1]
    query = select(GroupMember, User).join(User, User.id == GroupMember.user_id).where(
        GroupMember.group_id == callback.message.chat.id
    )
    if action in ("unmute", "tmute"):
        if action == "unmute":
            query = query.where(GroupMember.is_muted.is_(True))
    elif action in ("unban", "tban"):
        if action == "unban":
            query = query.where(GroupMember.is_banned.is_(True))
    query = query.order_by(GroupMember.last_activity.desc()).limit(25)
    rows = (await db_session.execute(query)).all()
    if not rows:
        message = "No tracked muted members found." if action == "unmute" else "No tracked banned members found." if action == "unban" else "No tracked members yet. Use the command by replying to a member's message, or try again after members have chatted."
        await callback.answer(message, show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    for member, user in rows:
        name = (user.first_name or user.username or str(user.id)).replace("\n", " ")[:30]
        kb.button(text=f"{name} · {user.id}", callback_data=f"mod:target:{action}:{user.id}")
    kb.button(text="◀️ Back", callback_data="mod:menu")
    kb.adjust(1)
    try:
        await callback.message.edit_text(f"Select a member to {action}:", reply_markup=kb.as_markup())
    except Exception:
        await callback.message.answer(f"Select a member to {action}:", reply_markup=kb.as_markup())
    await callback.answer()


@router.callback_query(F.data == "mod:history")
async def cb_history_info(callback: CallbackQuery, **kwargs):
    await callback.answer("Use /history by replying to a member's message, or /history <user ID>.", show_alert=True)


@router.callback_query(F.data == "mod:notes")
async def cb_notes_info(callback: CallbackQuery, **kwargs):
    await callback.answer("To add a note, reply to a member's message with /note <text>. Use /history to review moderation actions.", show_alert=True)


@router.callback_query(F.data == "mod:roles")
async def cb_roles_info(callback: CallbackQuery, **kwargs):
    await callback.answer("Reply to a member with /setrole <label>. Use /role to view it or /clearrole to remove it.", show_alert=True)


@router.callback_query(F.data == "mod:strike")
async def cb_strike_info(callback: CallbackQuery, **kwargs):
    await callback.answer("Use /warn to add a strike and /unwarn to remove the latest warning. Max-warning actions are configured in group settings.", show_alert=True)


@router.callback_query(F.data.startswith("mod:target:"))
async def cb_mod_target(callback: CallbackQuery, db_session: AsyncSession, **kwargs):
    from bot.keyboards.moderation import ban_duration_kb
    try:
        _, _, action, raw_id = callback.data.split(":", 3)
        user_id = int(raw_id)
    except (ValueError, IndexError):
        await callback.answer("Invalid member selection.", show_alert=True)
        return
    if callback.message.chat.type not in ("group", "supergroup"):
        await callback.answer("Group only.", show_alert=True)
        return
    try:
        actor = await callback.bot.get_chat_member(callback.message.chat.id, callback.from_user.id)
        if actor.status not in ("administrator", "creator"):
            await callback.answer("Only group admins can use moderation actions.", show_alert=True)
            return
    except Exception:
        await callback.answer("Could not verify your admin permissions.", show_alert=True)
        return

    group_id = callback.message.chat.id
    if action in ("mute", "tmute"):
        await callback.message.edit_text("Choose mute duration:", reply_markup=mute_duration_kb(None, user_id))
        await callback.answer()
        return
    if action in ("ban", "tban"):
        await callback.message.edit_text("Choose ban duration:", reply_markup=ban_duration_kb(None, user_id))
        await callback.answer()
        return
    if action == "unmute":
        ok = await unmute_user(callback.bot, db_session, group_id, user_id, callback.from_user.id)
        message = "✅ Member unmuted." if ok else "❌ Unmute failed. Check ALEXA's Restrict Members permission and confirm the member is still in the group."
    elif action == "unban":
        ok = await unban_user(callback.bot, db_session, group_id, user_id, callback.from_user.id)
        message = "✅ Member unbanned." if ok else "❌ Unban failed. Check ALEXA's Ban Users permission."
    elif action == "kick":
        ok = await kick_user(callback.bot, db_session, group_id, user_id, callback.from_user.id, "Selected from moderation menu")
        message = "✅ Member kicked." if ok else "❌ Kick failed. Check ALEXA's Ban Users permission."
    elif action == "unwarn":
        ok = await unwarn_user(db_session, group_id, user_id)
        message = "✅ Latest warning removed." if ok else "No warning found for this member."
    elif action == "warn":
        count, reached = await warn_user(db_session, group_id, user_id, callback.from_user.id, "Selected from moderation menu")
        message = f"⚠️ Warning recorded ({count})."
        if reached:
            gs = await db_session.scalar(select(GroupSettings).where(GroupSettings.group_id == group_id))
            warn_action = gs.warn_action if gs else "mute"
            if warn_action == "mute":
                await mute_user(callback.bot, db_session, group_id, user_id, callback.from_user.id, 3600, "Warning limit reached")
            elif warn_action == "kick":
                await kick_user(callback.bot, db_session, group_id, user_id, callback.from_user.id, "Warning limit reached")
            elif warn_action == "ban":
                await ban_user(callback.bot, db_session, group_id, user_id, callback.from_user.id, "Warning limit reached")
    else:
        await callback.answer("Unknown moderation action.", show_alert=True)
        return
    try:
        await callback.message.edit_text(message, reply_markup=back_button_kb("mod:menu"))
    except Exception:
        await callback.answer(message, show_alert=True)
        return
    await callback.answer()


@router.message(Command("setrole"))
async def cmd_setrole(message: Message, _: callable, db_session: AsyncSession, **kwargs):
    if message.chat.type == "private":
        await message.reply("Groups only.")
        return
    if not message.reply_to_message or not message.reply_to_message.from_user:
        await message.reply("Reply to a member and use /setrole <role name>.")
        return
    parts = (message.text or "").split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply("Usage: reply to a member with /setrole <role name>.")
        return
    target_tg = message.reply_to_message.from_user
    target = await db_session.get(User, target_tg.id)
    if not target:
        target = User(id=target_tg.id, telegram_id=target_tg.id, username=target_tg.username, first_name=target_tg.first_name or "User", last_name=target_tg.last_name, is_bot=target_tg.is_bot)
        db_session.add(target)
        await db_session.flush()
    member = await db_session.scalar(select(GroupMember).where(GroupMember.group_id == message.chat.id, GroupMember.user_id == target_tg.id))
    if not member:
        member = GroupMember(group_id=message.chat.id, user_id=target_tg.id)
        db_session.add(member)
    member.custom_role = parts[1].strip()[:64]
    await db_session.flush()
    await message.reply(f"✅ Custom label set for {safe_username(target)}: <b>{escape(member.custom_role)}</b>", parse_mode="HTML")


@router.message(Command("clearrole"))
async def cmd_clearrole(message: Message, _: callable, db_session: AsyncSession, **kwargs):
    if message.chat.type == "private" or not message.reply_to_message or not message.reply_to_message.from_user:
        await message.reply("Reply to a member in a group and use /clearrole.")
        return
    target_id = message.reply_to_message.from_user.id
    member = await db_session.scalar(select(GroupMember).where(GroupMember.group_id == message.chat.id, GroupMember.user_id == target_id))
    if not member or not member.custom_role:
        await message.reply("This member has no custom label.")
        return
    member.custom_role = None
    await db_session.flush()
    await message.reply("✅ Custom label removed.")


@router.message(Command("role"))
async def cmd_role(message: Message, _: callable, db_session: AsyncSession, **kwargs):
    if message.chat.type == "private" or not message.reply_to_message or not message.reply_to_message.from_user:
        await message.reply("Reply to a member in a group and use /role.")
        return
    target_id = message.reply_to_message.from_user.id
    member = await db_session.scalar(select(GroupMember).where(GroupMember.group_id == message.chat.id, GroupMember.user_id == target_id))
    role = member.custom_role if member and member.custom_role else "No custom label"
    await message.reply(f"Custom label: <b>{escape(role)}</b>", parse_mode="HTML")


@router.message(Command("resetwarns"))
async def cmd_resetwarns(message: Message, _: callable, db_session: AsyncSession, **kwargs):
    if message.chat.type == "private":
        await message.reply("Groups only."); return
    target, _reason = await get_target_user(message, db_session)
    if not target:
        await message.reply("Reply to a member or use /resetwarns <user ID>."); return
    await db_session.execute(delete(Warning).where(Warning.group_id == message.chat.id, Warning.user_id == target.id))
    member = await db_session.scalar(select(GroupMember).where(GroupMember.group_id == message.chat.id, GroupMember.user_id == target.id))
    if member: member.warn_count = 0
    await db_session.flush()
    await message.reply(f"✅ Warnings reset for {escape(safe_username(target))}.", parse_mode="HTML")


@router.message(Command("promote"))
async def cmd_promote(message: Message, bot: Bot, db_session: AsyncSession, **kwargs):
    if message.chat.type == "private":
        await message.reply("Groups only."); return
    target, _ = await get_target_user(message, db_session)
    if not target:
        await message.reply("Reply to a member or use /promote <user ID>."); return
    try:
        await bot.promote_chat_member(message.chat.id, target.id, can_manage_chat=True, can_delete_messages=True, can_manage_video_chats=True, can_restrict_members=True, can_promote_members=False, can_change_info=False, can_invite_users=True, can_pin_messages=True, can_post_messages=False, can_edit_messages=False, can_manage_topics=True)
        await message.reply(f"✅ Promoted {escape(safe_username(target))} with moderation permissions.", parse_mode="HTML")
    except Exception as exc:
        await message.reply(f"❌ Promotion failed. Check the bot's admin rights. ({escape(str(exc)[:180])})", parse_mode="HTML")


@router.message(Command("demote"))
async def cmd_demote(message: Message, bot: Bot, db_session: AsyncSession, **kwargs):
    if message.chat.type == "private":
        await message.reply("Groups only."); return
    target, _ = await get_target_user(message, db_session)
    if not target:
        await message.reply("Reply to an admin or use /demote <user ID>."); return
    try:
        await bot.promote_chat_member(message.chat.id, target.id, can_manage_chat=False, can_delete_messages=False, can_manage_video_chats=False, can_restrict_members=False, can_promote_members=False, can_change_info=False, can_invite_users=False, can_pin_messages=False, can_post_messages=False, can_edit_messages=False, can_manage_topics=False)
        await message.reply(f"✅ Demoted {escape(safe_username(target))}.", parse_mode="HTML")
    except Exception as exc:
        await message.reply(f"❌ Demotion failed. Check the bot's admin rights. ({escape(str(exc)[:180])})", parse_mode="HTML")


@router.message(Command("del"))
async def cmd_delete_message(message: Message, bot: Bot, **kwargs):
    if message.chat.type == "private" or not message.reply_to_message:
        await message.reply("Reply to the message you want to delete with /del."); return
    try:
        await bot.delete_message(message.chat.id, message.reply_to_message.message_id)
        await bot.delete_message(message.chat.id, message.message_id)
    except Exception as exc:
        await message.reply(f"Delete failed. Check bot admin/delete permissions. ({escape(str(exc)[:150])})", parse_mode="HTML")



@router.message(Command("purg"))
async def cmd_purg(message: Message, bot: Bot, **kwargs):
    if message.chat.type not in ("group", "supergroup"):
        return
    if not message.reply_to_message:
        await message.reply("🧹 Reply to the first message you want to delete with /purg.")
        return
    start_id = message.reply_to_message.message_id
    end_id = message.message_id
    if end_id < start_id or end_id - start_id + 1 > 1000:
        await message.reply("Purge range is too large. Select a range of up to 1000 message IDs.")
        return
    try:
        me = await bot.get_chat_member(message.chat.id, bot.id)
        status = str(getattr(me.status, "value", me.status))
        if status not in {"administrator", "creator"} or (status == "administrator" and not getattr(me, "can_delete_messages", False)):
            await message.reply("ALEXA needs admin + Delete Messages permission to purge.")
            return
    except Exception:
        await message.reply("Could not verify ALEXA's delete-message permission.")
        return
    ids = list(range(start_id, end_id + 1))
    deleted = failed = 0
    for offset in range(0, len(ids), 100):
        batch = ids[offset:offset + 100]
        try:
            await bot.delete_messages(chat_id=message.chat.id, message_ids=batch)
            deleted += len(batch)
        except Exception:
            for message_id in batch:
                try:
                    await bot.delete_message(chat_id=message.chat.id, message_id=message_id)
                    deleted += 1
                except Exception:
                    failed += 1
    await message.answer(
        f"🧹 <b>Purge finished</b>\\nDeleted: <b>{deleted}</b>\\n"
        f"Could not delete: <b>{failed}</b>\\n"
        "Telegram may not allow deleting messages older than 48 hours.",
        parse_mode="HTML",
    )


@router.message(Command("pin"))
async def cmd_pin(message: Message, bot: Bot, **kwargs):
    if message.chat.type == "private" or not message.reply_to_message:
        await message.reply("Reply to a message with /pin."); return
    try:
        await bot.pin_chat_message(message.chat.id, message.reply_to_message.message_id, disable_notification=True)
        await message.reply("📌 Message pinned.")
    except Exception as exc:
        await message.reply(f"Pin failed. Check bot permissions. ({escape(str(exc)[:150])})", parse_mode="HTML")


@router.message(Command("unpin"))
async def cmd_unpin(message: Message, bot: Bot, **kwargs):
    if message.chat.type == "private":
        await message.reply("Groups only."); return
    try:
        if message.reply_to_message:
            await bot.unpin_chat_message(message.chat.id, message.reply_to_message.message_id)
        else:
            await bot.unpin_chat_message(message.chat.id)
        await message.reply("📌 Message unpinned.")
    except Exception as exc:
        await message.reply(f"Unpin failed. Check bot permissions. ({escape(str(exc)[:150])})", parse_mode="HTML")


@router.message(Command("history"))
async def cmd_history(message: Message, _: callable, db_session: AsyncSession, **kwargs):
    if message.chat.type == "private":
        return
    target, _ = await get_target_user(message, db_session)
    if not target:
        await message.reply(_("provide_user"))
        return

    history = await get_user_history(db_session, message.chat.id, target.id)
    member = history["member"]
    logs = history["logs"]
    warnings = history["warnings"]

    text = _("user_history_title").format(name=safe_username(target)) + "\n\n"
    text += _("history_warnings").format(count=len(warnings)) + "\n"
    if member:
        text += _("history_messages").format(count=member.message_count or 0) + "\n"
        if member.joined_at:
            text += _("history_joined").format(date=member.joined_at.strftime("%Y-%m-%d")) + "\n"

    ban_count = sum(1 for l in logs if l.action == "ban")
    mute_count = sum(1 for l in logs if l.action == "mute")
    kick_count = sum(1 for l in logs if l.action == "kick")
    text += _("history_bans").format(count=ban_count) + "\n"
    text += _("history_mutes").format(count=mute_count) + "\n"
    text += _("history_kicks").format(count=kick_count) + "\n"

    await message.reply(text, parse_mode="HTML", reply_markup=back_button_kb(_, "mod:menu"))
