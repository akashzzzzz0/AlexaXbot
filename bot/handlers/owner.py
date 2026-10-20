import asyncio
import time
from aiogram import Router, F, Bot
from aiogram.types import CallbackQuery, Message, BotCommand, BotCommandScopeDefault, BotCommandScopeChat, ReplyParameters
from aiogram.exceptions import TelegramRetryAfter, TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc
from aiogram.utils.keyboard import InlineKeyboardBuilder
from bot.database.models import User, Group, GroupMember, GroupSettings, ActionLog, SecuritySettings, BioLinkProtectionSetting, BioLinkProtectionConfig, ScamReview, InboxMessage
from bot.keyboards.main_menu import back_button_kb
from bot.config import settings
from bot.utils.helpers import format_number
import structlog
from html import escape

logger = structlog.get_logger()
router = Router()

_maintenance_mode = False
_PROCESS_STARTED = time.monotonic()


class OwnerStates(StatesGroup):
    waiting_broadcast = State()
    waiting_group_promotion = State()
    waiting_unban_user_id = State()
    waiting_unmute_user_id = State()
    waiting_purge_range = State()
    waiting_inbox_reply = State()


def is_owner(user_id: int) -> bool:
    return bool(settings.effective_owner_id) and user_id == settings.effective_owner_id


def owner_kb(_=None) -> object:
    """Cyber Neon Pro layout; existing callback_data values are intentionally preserved."""
    builder = InlineKeyboardBuilder()
    # Broadcast & promotion
    builder.button(text="📨 DM Broadcast", callback_data="owner:broadcast")
    builder.button(text="📣 Group Promotion", callback_data="owner:promotion")
    # Global overview & analytics
    builder.button(text="🌐 Global Controls", callback_data="owner:global_controls")
    builder.button(text="📊 Statistics", callback_data="owner:stats")
    builder.button(text="🩺 Bot Health Monitor", callback_data="owner:health")
    # Group network & rewards
    builder.button(text="👥 Groups • Activity • Links • Permissions", callback_data="owner:groups")
    builder.button(text="🔗 Force Join", callback_data="owner:forcejoin")
    builder.button(text="🎁 Affiliate / Referral", callback_data="owner:referrals")
    # Moderation & configuration
    builder.button(text="🔨 Ban All", callback_data="owner:banall")
    builder.button(text="⚙️ Settings", callback_data="owner:settings")
    # Administration & protection
    builder.button(text="➕ Add Groups", callback_data="owner:addgroups")
    builder.button(text=f"{'🟢 Disable' if _maintenance_mode else '🔧 Enable'} Maintenance", callback_data="owner:maintenance")
    builder.button(text="👤 User List", callback_data="owner:users")
    builder.button(text="📩 User Messages", callback_data="owner:inbox")
    builder.button(text="🚨 Emergency Controls", callback_data="owner:emergency")
    builder.adjust(2, 2, 2, 2, 2, 2, 2, 1)
    return builder.as_markup()


# NOTE: this function is intentionally called only by the bot owner.
async def _active_groups(session: AsyncSession):
    result = await session.execute(
        select(Group).where(Group.is_active.is_(True)).order_by(Group.title.asc())
    )
    return result.scalars().all()


def _group_list_kb(groups, action: str):
    builder = InlineKeyboardBuilder()
    for group in groups:
        builder.button(text=f"👥 {group.title[:48]}", callback_data=f"owner:{action}:{group.id}")
    builder.button(text="◀️ Back to Owner Panel", callback_data="owner:menu")
    builder.adjust(1)
    return builder.as_markup()


async def _emergency_groups_kb(groups, session: AsyncSession):
    builder = InlineKeyboardBuilder()
    for group in groups:
        security = await session.scalar(
            select(SecuritySettings).where(SecuritySettings.group_id == group.id)
        )
        enabled = bool(security and security.emergency_mode)
        status = "🔴 LOCKDOWN ON" if enabled else "🟢 LOCKDOWN OFF"
        builder.button(
            text=f"{status} · {group.title[:30]}",
            callback_data=f"owner:emergency:{group.id}",
        )
    builder.button(text="◀️ Back to Owner Panel", callback_data="owner:menu")
    builder.adjust(1)
    return builder.as_markup()


def _emergency_detail_kb(group_id: int, enabled: bool):
    builder = InlineKeyboardBuilder()
    builder.button(
        text="🔓 Disable Lockdown" if enabled else "🚨 Activate Lockdown",
        callback_data=f"owner:emergency:{group_id}",
    )
    builder.button(text="🔄 Refresh Group Status", callback_data=f"owner:emergency_view:{group_id}")
    builder.button(text="📋 All Groups", callback_data="owner:emergency")
    builder.button(text="👑 Owner Panel", callback_data="owner:menu")
    builder.adjust(1, 1, 2)
    return builder.as_markup()


def _group_detail_kb(group_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(text="🛡️ Security Settings", callback_data=f"owner:security:{group_id}")
    builder.button(text="🔒 Content Locks", callback_data=f"owner:locks:{group_id}")
    builder.button(text="🛡️ Bio Link Protection", callback_data=f"owner:biolink:{group_id}")
    builder.button(text="🚨 Emergency Lockdown", callback_data=f"owner:emergency_view:{group_id}")
    builder.button(text="📈 Activity", callback_data=f"owner:activity:{group_id}")
    builder.button(text="🔗 Group Link", callback_data=f"owner:link:{group_id}")
    builder.button(text="🧹 Purge Messages", callback_data=f"owner:purge:{group_id}")
    builder.button(text="🔕 Join/Leave/Ban Messages: Toggle", callback_data=f"owner:silent_service:{group_id}")
    builder.button(text="🔓 Unban Member", callback_data=f"owner:banlist:{group_id}")
    builder.button(text="🆔 Unban by User ID", callback_data=f"owner:unbanid:{group_id}")
    builder.button(text="🔇 Unmute by User ID", callback_data=f"owner:unmuteid:{group_id}")
    builder.button(text="🔨 Ban All", callback_data=f"owner:banall:{group_id}")
    builder.button(text="🔄 Refresh", callback_data=f"owner:group:{group_id}")
    builder.button(text="◀️ Groups", callback_data="owner:groups")
    builder.adjust(1, 1, 1, 1, 2, 2, 1)
    return builder.as_markup()


async def _render_group(callback: CallbackQuery, session: AsyncSession, group_id: int):
    group = await session.get(Group, group_id)
    if not group or not group.is_active:
        await callback.answer("Group is not active/registered.", show_alert=True)
        return
    count = await session.scalar(
        select(func.count()).select_from(GroupMember).where(GroupMember.group_id == group_id)
    )
    action_count = await session.scalar(
        select(func.count()).select_from(ActionLog).where(ActionLog.group_id == group_id)
    )
    recent_logs = (await session.scalars(
        select(ActionLog).where(ActionLog.group_id == group_id)
        .order_by(desc(ActionLog.created_at)).limit(3)
    )).all()
    recent_activity = "\n".join(
        f"• {escape(log.action)} — {escape(log.created_at.strftime('%m-%d %H:%M') if log.created_at else 'time unknown')}"
        for log in recent_logs
    ) or "• No activity logs yet."
    link = f"https://t.me/{group.username.lstrip('@')}" if group.username else (group.invite_link or "")
    permissions = []
    try:
        me = await callback.bot.get_chat_member(group_id, callback.bot.id)
        status = str(getattr(me.status, "value", me.status))
        if status == "creator":
            permissions = ["Group creator"]
        elif status == "administrator":
            checks = [
                ("Delete messages", "can_delete_messages"), ("Restrict members", "can_restrict_members"),
                ("Invite users", "can_invite_users"), ("Pin messages", "can_pin_messages"),
                ("Change group info", "can_change_info"), ("Promote admins", "can_promote_members"),
                ("Manage topics", "can_manage_topics"), ("Manage video chats", "can_manage_video_chats"),
            ]
            permissions = [label for label, attr in checks if getattr(me, attr, False)]
            if not link and getattr(me, "can_invite_users", False):
                try:
                    invite = await callback.bot.create_chat_invite_link(group_id, name="ALEXA Owner Panel")
                    link = invite.invite_link
                    group.invite_link = link
                except Exception:
                    pass
        else:
            permissions = ["Not an administrator"]
    except Exception as exc:
        logger.warning("Could not fetch bot group permissions", group_id=group_id, error=str(exc))
        permissions = ["Could not fetch permissions"]
    if not link:
        link = "Private link unavailable (grant Invite Users permission)"
    safe_link = escape(link)
    safe_title = escape(group.title or "Untitled")
    title_html = f'<a href="{safe_link}"><b>{safe_title}</b></a>' if link.startswith("https://") else f"<b>{safe_title}</b>"
    text = (
        f"👥 {title_html}\n\n"
        f"🆔 <code>{group.id}</code>\n"
        f"📌 Type: <code>{escape(group.type)}</code>\n"
        f"👤 Tracked members: <b>{format_number(count or 0)}</b>\n"
        f"📝 Logged activities: <b>{format_number(action_count or 0)}</b>\n"
        f"📈 <b>Recent activity</b>\n{recent_activity}\n"
        f"🤖 ALEXA status: <b>ACTIVE</b>\n"
        f"🔗 Group link: {safe_link}\n\n"
        f"🛡️ <b>ALEXA permissions</b>\n" + ("\n".join(f"• {escape(item)}" for item in permissions) if permissions else "• None reported")
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=_group_detail_kb(group_id), disable_web_page_preview=True)


@router.message(Command("owner", "panel"))
async def owner_panel(message: Message, _: callable, **kwargs):
    if not is_owner(message.from_user.id) or message.chat.type != "private":
        # Never post the owner panel into a group, even if the owner invokes it there.
        return
    await message.answer(
        "💠 <b>ALEXA X</b>  <code>CYBER NEON PRO</code>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "👑 <b>OWNER ACCESS: VERIFIED</b>\n"
        "⚡ <b>ULTRA CONTROL DECK</b>\n\n"
        "📡 <b>CONTROL MODULES</b>\n"
        "Broadcast • Promotion • Global Controls • Statistics\n"
        "Groups • Referrals • Ban All • Settings\n"
        "Maintenance • User List • User Messages • Emergency Controls\n\n"
        "🔹 Select a module below. Existing actions and permissions are retained.",
        reply_markup=owner_kb(),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "owner:settings")
async def owner_settings(callback: CallbackQuery, _: callable, db_session: AsyncSession, **kwargs):
    if not is_owner(callback.from_user.id):
        await callback.answer("Not available.", show_alert=True)
        return
    user_count = await db_session.scalar(select(func.count()).select_from(User).where(User.dm_started.is_(True), User.is_banned.is_(False)))
    group_count = await db_session.scalar(
        select(func.count()).select_from(Group).where(Group.is_active.is_(True))
    )
    builder = InlineKeyboardBuilder()
    builder.button(text=f"{'🟢 Turn maintenance OFF' if _maintenance_mode else '🔧 Turn maintenance ON'}", callback_data="owner:maintenance")
    builder.button(text="🚨 Emergency Controls", callback_data="owner:emergency")
    builder.button(text="📣 Group Promotion", callback_data="owner:promotion")
    builder.button(text="👥 Groups • Activity • Links • Permissions", callback_data="owner:groups")
    builder.button(text="◀️ Owner Panel", callback_data="owner:menu")
    builder.adjust(1, 2, 1, 1)
    await callback.message.edit_text(
        "⚙️ <b>ALEXA OWNER SETTINGS</b>\n\n"
        f"Maintenance: <b>{'ON' if _maintenance_mode else 'OFF'}</b>\n"
        f"DM starters: <b>{format_number(user_count or 0)}</b>\n"
        f"Active groups: <b>{format_number(group_count or 0)}</b>\n\n"
        "Choose a control below. These controls are restricted to the configured bot owner.",
        parse_mode="HTML", reply_markup=builder.as_markup()
    )
    await callback.answer()


@router.callback_query(F.data == "owner:health")
async def owner_health(callback: CallbackQuery, db_session: AsyncSession, **kwargs):
    if not is_owner(callback.from_user.id):
        await callback.answer("Not available.", show_alert=True)
        return
    db_status = "🟢 Connected"
    try:
        await db_session.execute(select(1))
        active_groups = await db_session.scalar(
            select(func.count()).select_from(Group).where(Group.is_active.is_(True))
        )
        dm_users = await db_session.scalar(
            select(func.count()).select_from(User).where(User.dm_started.is_(True))
        )
        pending_reviews = await db_session.scalar(
            select(func.count()).select_from(ScamReview).where(ScamReview.status == "pending")
        )
    except Exception as exc:
        await db_session.rollback()
        db_status = "🔴 Check failed"
        active_groups = dm_users = pending_reviews = 0
        logger.warning("Owner health database check failed", error=str(exc))
    uptime_seconds = max(0, int(time.monotonic() - _PROCESS_STARTED))
    hours, remainder = divmod(uptime_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    uptime = f"{hours}h {minutes}m {seconds}s"
    text = (
        "🩺 <b>ALEXA BOT HEALTH MONITOR</b>\n\n"
        "🟢 Process: <b>Running</b>\n"
        f"🗄️ Database: <b>{db_status}</b>\n"
        f"⏱️ Process uptime: <code>{uptime}</code>\n"
        f"👥 Active groups: <b>{format_number(active_groups or 0)}</b>\n"
        f"👤 DM starters: <b>{format_number(dm_users or 0)}</b>\n"
        f"🛡️ Pending scam reviews: <b>{format_number(pending_reviews or 0)}</b>\n\n"
        "This is a live snapshot, not a guarantee of uninterrupted hosting. "
        "The external /health endpoint remains available when the host keeps the process awake."
    )
    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 Refresh", callback_data="owner:health")
    builder.button(text="◀️ Owner Panel", callback_data="owner:menu")
    builder.adjust(2)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=builder.as_markup())
    await callback.answer()


@router.callback_query(F.data == "owner:stats")
async def owner_stats(callback: CallbackQuery, _: callable, db_session: AsyncSession, **kwargs):
    if not is_owner(callback.from_user.id):
        await callback.answer("Not available.", show_alert=True)
        return
    user_count = await db_session.scalar(select(func.count()).select_from(User).where(User.dm_started.is_(True), User.is_banned.is_(False)))
    group_count = await db_session.scalar(
        select(func.count()).select_from(Group).where(Group.is_active.is_(True))
    )
    member_count = await db_session.scalar(select(func.count()).select_from(GroupMember))
    text = (
        "📊 <b>ALEXA Global Statistics</b>\n\n"
        f"👤 DM starters: <b>{format_number(user_count or 0)}</b>\n"
        f"👥 Active groups: <b>{format_number(group_count or 0)}</b>\n"
        f"🗃️ Tracked group memberships: <b>{format_number(member_count or 0)}</b>\n"
        f"🔧 Maintenance: <b>{'ON' if _maintenance_mode else 'OFF'}</b>"
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=back_button_kb(_, "owner:menu"))
    await callback.answer()


@router.callback_query(F.data == "owner:groups")
async def owner_groups(callback: CallbackQuery, _: callable, db_session: AsyncSession, **kwargs):
    if not is_owner(callback.from_user.id):
        await callback.answer("Not available.", show_alert=True)
        return
    groups = await _active_groups(db_session)
    if not groups:
        await callback.message.edit_text(
            "📋 <b>Active Groups</b>\n\nNo active groups are registered yet.",
            parse_mode="HTML",
            reply_markup=back_button_kb(_, "owner:menu"),
        )
    else:
        await callback.message.edit_text(
            "📋 <b>Active Groups</b>\n\nTap a group to manage it.",
            parse_mode="HTML",
            reply_markup=_group_list_kb(groups, "group"),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("owner:group:"))
async def owner_group(callback: CallbackQuery, _: callable, db_session: AsyncSession, **kwargs):
    if not is_owner(callback.from_user.id):
        await callback.answer("Not available.", show_alert=True)
        return
    try:
        group_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Invalid group.", show_alert=True)
        return
    await _render_group(callback, db_session, group_id)
    await callback.answer()



def _owner_security_fields():
    return {
        "anti_spam": ("Anti-Spam", "anti_spam"),
        "anti_flood": ("Anti-Flood", "anti_flood"),
        "anti_raid": ("Anti-Raid", "anti_raid"),
        "anti_bot": ("Anti-Bot", "anti_bot"),
        "anti_link": ("Anti-Link", "anti_link"),
        "anti_scam": ("Anti-Scam", "anti_scam"),
        "anti_invite": ("Anti-Invite Links", "anti_invite"),
        "anti_advertisement": ("Anti-Advertisement", "anti_advertisement"),
    }


def _owner_security_kb(group_id: int, security: SecuritySettings, locks: bool = False):
    builder = InlineKeyboardBuilder()
    if locks:
        fields = {
            "lock_text": "Text", "lock_links": "Links", "lock_photos": "Photos",
            "lock_videos": "Videos", "lock_gifs": "GIFs", "lock_audio": "Audio",
            "lock_voice": "Voice", "lock_documents": "Documents", "lock_polls": "Polls",
            "lock_games": "Games", "lock_bots": "Bots", "lock_forwards": "Forwards",
            "lock_contacts": "Contacts", "lock_locations": "Locations", "lock_stickers": "Stickers",
            "lock_inline": "Inline buttons", "lock_mentions": "Mentions", "lock_hashtags": "Hashtags",
        }
    else:
        fields = {key: value[0] for key, value in _owner_security_fields().items()}
        fields["emergency_mode"] = "Emergency Lockdown"
    for field, label in fields.items():
        enabled = bool(getattr(security, field, False))
        builder.button(
            text=f"{'🟢' if enabled else '🔴'} {label}: {'ON' if enabled else 'OFF'}",
            callback_data=f"owner:security_toggle:{group_id}:{field}",
        )
    builder.button(text="◀️ Group Dashboard", callback_data=f"owner:group:{group_id}")
    builder.button(text="👑 Owner Panel", callback_data="owner:menu")
    builder.adjust(1)
    return builder.as_markup()


async def _render_owner_security(callback: CallbackQuery, session: AsyncSession, group_id: int, locks: bool = False):
    group = await session.get(Group, group_id)
    if not group or not group.is_active:
        await callback.answer("Group is not active/registered.", show_alert=True)
        return
    security = await session.scalar(select(SecuritySettings).where(SecuritySettings.group_id == group_id))
    if security is None:
        security = SecuritySettings(group_id=group_id)
        session.add(security)
        await session.flush()
    title = "Content Locks" if locks else "Security Settings"
    description = (
        "Toggle the content types ALEXA should block in this group."
        if locks else
        "Configure this group's security independently. Changes save to this group only."
    )
    await callback.message.edit_text(
        f"🛡️ <b>{escape(title)} — {escape(group.title)}</b>\n\n{description}\n\n"
        "🟢 ON  •  🔴 OFF",
        parse_mode="HTML",
        reply_markup=_owner_security_kb(group_id, security, locks=locks),
    )


def _owner_biolink_kb(group_id: int, enabled: bool, action: str, duration: int):
    builder = InlineKeyboardBuilder()
    builder.button(
        text=("🟢 Protection ON" if enabled else "🔴 Protection OFF"),
        callback_data=f"owner:biolink_toggle:{group_id}",
    )
    for value, label in (
        ("warn_mute", "⚠️ 3 warnings → Mute"),
        ("warn_ban", "⚠️ 3 warnings → Ban"),
        ("mute", "Immediate Mute"),
        ("ban", "Immediate Ban"),
    ):
        prefix = "✅ " if action == value else ""
        builder.button(text=prefix + label, callback_data=f"owner:biolink_action:{group_id}:{value}")
    for seconds, label in ((900, "15m"), (1800, "30m"), (3600, "1h"), (21600, "6h"), (86400, "24h")):
        prefix = "✅ " if duration == seconds else ""
        builder.button(
            text=f"{prefix}Mute duration: {label}",
            callback_data=f"owner:biolink_duration:{group_id}:{seconds}",
        )
    builder.button(text="◀️ Back to Group", callback_data=f"owner:group:{group_id}")
    builder.adjust(1, 2, 2, 2, 2, 1)
    return builder.as_markup()


async def _owner_render_biolink(callback: CallbackQuery, session: AsyncSession, group_id: int):
    group = await session.get(Group, group_id)
    if not group or not group.is_active:
        await callback.answer("Group is not active.", show_alert=True)
        return
    setting = await session.scalar(
        select(BioLinkProtectionSetting).where(BioLinkProtectionSetting.group_id == group_id)
    )
    config = await session.scalar(
        select(BioLinkProtectionConfig).where(BioLinkProtectionConfig.group_id == group_id)
    )
    if setting is None:
        setting = BioLinkProtectionSetting(group_id=group_id, enabled=False)
        session.add(setting)
    if config is None:
        config = BioLinkProtectionConfig(group_id=group_id, action="warn_mute", mute_duration=3600)
        session.add(config)
    await session.flush()
    labels = {
        "warn_mute": "3 warnings then mute",
        "warn_ban": "3 warnings then ban",
        "mute": "immediate mute",
        "ban": "immediate ban",
        "warn": "warnings only",
    }
    await callback.message.edit_text(
        f"🛡️ <b>Bio Link Protection — {escape(group.title)}</b>\n\n"
        f"Status: <b>{'ON' if setting.enabled else 'OFF'}</b>\n"
        f"Action: <b>{escape(labels.get(config.action, config.action))}</b>\n"
        f"Mute duration: <b>{max(1, config.mute_duration // 60)} minutes</b>\n\n"
        "Manage this group's Bio Link Protection from the Owner Panel.",
        parse_mode="HTML",
        reply_markup=_owner_biolink_kb(group_id, bool(setting.enabled), config.action, config.mute_duration),
    )


@router.callback_query(F.data.startswith("owner:biolink:"))
async def owner_biolink_open(callback: CallbackQuery, db_session: AsyncSession, **kwargs):
    if not is_owner(callback.from_user.id):
        await callback.answer("Not available.", show_alert=True)
        return
    try:
        group_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Invalid group.", show_alert=True)
        return
    await _owner_render_biolink(callback, db_session, group_id)
    await callback.answer()


@router.callback_query(F.data.startswith("owner:biolink_toggle:"))
async def owner_biolink_toggle(callback: CallbackQuery, db_session: AsyncSession, **kwargs):
    if not is_owner(callback.from_user.id):
        await callback.answer("Not available.", show_alert=True)
        return
    try:
        group_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Invalid group.", show_alert=True)
        return
    setting = await db_session.scalar(
        select(BioLinkProtectionSetting).where(BioLinkProtectionSetting.group_id == group_id)
    )
    if setting is None:
        setting = BioLinkProtectionSetting(group_id=group_id, enabled=False)
        db_session.add(setting)
    setting.enabled = not bool(setting.enabled)
    await db_session.flush()
    await _owner_render_biolink(callback, db_session, group_id)
    await callback.answer("Bio Link Protection updated.")


@router.callback_query(F.data.startswith("owner:biolink_action:"))
async def owner_biolink_action(callback: CallbackQuery, db_session: AsyncSession, **kwargs):
    if not is_owner(callback.from_user.id):
        await callback.answer("Not available.", show_alert=True)
        return
    try:
        parts = callback.data.split(":", 3)
        group_id, action = int(parts[2]), parts[3]
    except (ValueError, TypeError):
        await callback.answer("Invalid setting.", show_alert=True)
        return
    if action not in {"warn_mute", "warn_ban", "mute", "ban"}:
        await callback.answer("Invalid action.", show_alert=True)
        return
    config = await db_session.scalar(
        select(BioLinkProtectionConfig).where(BioLinkProtectionConfig.group_id == group_id)
    )
    if config is None:
        config = BioLinkProtectionConfig(group_id=group_id, action=action, mute_duration=3600)
        db_session.add(config)
    else:
        config.action = action
    await db_session.flush()
    await _owner_render_biolink(callback, db_session, group_id)
    await callback.answer("Bio Link action updated.")


@router.callback_query(F.data.startswith("owner:biolink_duration:"))
async def owner_biolink_duration(callback: CallbackQuery, db_session: AsyncSession, **kwargs):
    if not is_owner(callback.from_user.id):
        await callback.answer("Not available.", show_alert=True)
        return
    try:
        parts = callback.data.split(":", 3)
        group_id, duration = int(parts[2]), int(parts[3])
    except (ValueError, TypeError):
        await callback.answer("Invalid duration.", show_alert=True)
        return
    if duration not in {900, 1800, 3600, 21600, 86400}:
        await callback.answer("Invalid duration.", show_alert=True)
        return
    config = await db_session.scalar(
        select(BioLinkProtectionConfig).where(BioLinkProtectionConfig.group_id == group_id)
    )
    if config is None:
        config = BioLinkProtectionConfig(group_id=group_id, action="warn_mute", mute_duration=duration)
        db_session.add(config)
    else:
        config.mute_duration = duration
    await db_session.flush()
    await _owner_render_biolink(callback, db_session, group_id)
    await callback.answer("Mute duration updated.")



@router.callback_query(F.data.startswith("owner:security:"))
async def owner_security_open(callback: CallbackQuery, db_session: AsyncSession, **kwargs):
    if not is_owner(callback.from_user.id):
        await callback.answer("Not available.", show_alert=True); return
    try: group_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Invalid group.", show_alert=True); return
    await _render_owner_security(callback, db_session, group_id, locks=False)
    await callback.answer()


@router.callback_query(F.data.startswith("owner:locks:"))
async def owner_locks_open(callback: CallbackQuery, db_session: AsyncSession, **kwargs):
    if not is_owner(callback.from_user.id):
        await callback.answer("Not available.", show_alert=True); return
    try: group_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Invalid group.", show_alert=True); return
    await _render_owner_security(callback, db_session, group_id, locks=True)
    await callback.answer()


@router.callback_query(F.data.startswith("owner:security_toggle:"))
async def owner_security_toggle(callback: CallbackQuery, db_session: AsyncSession, **kwargs):
    if not is_owner(callback.from_user.id):
        await callback.answer("Not available.", show_alert=True); return
    try:
        _, _, group_id_text, field = callback.data.split(":", 3)
        group_id = int(group_id_text)
    except (ValueError, TypeError):
        await callback.answer("Invalid setting.", show_alert=True); return
    allowed = {key for key in _owner_security_fields()}
    allowed.update({
        "emergency_mode", "lock_text", "lock_links", "lock_photos", "lock_videos",
        "lock_gifs", "lock_audio", "lock_voice", "lock_documents", "lock_polls",
        "lock_games", "lock_bots", "lock_forwards", "lock_contacts", "lock_locations",
        "lock_stickers", "lock_inline", "lock_mentions", "lock_hashtags",
    })
    if field not in allowed:
        await callback.answer("Unknown setting.", show_alert=True); return
    group = await db_session.get(Group, group_id)
    if not group or not group.is_active:
        await callback.answer("Group is not active.", show_alert=True); return
    security = await db_session.scalar(select(SecuritySettings).where(SecuritySettings.group_id == group_id))
    if security is None:
        security = SecuritySettings(group_id=group_id); db_session.add(security); await db_session.flush()
    setattr(security, field, not bool(getattr(security, field, False)))
    try: await db_session.commit()
    except Exception as exc:
        await db_session.rollback()
        logger.warning("owner_group_security_toggle_failed", group_id=group_id, field=field, error=str(exc))
        await callback.answer("Could not save this setting.", show_alert=True); return
    await _render_owner_security(callback, db_session, group_id, locks=field.startswith("lock_"))
    await callback.answer(f"{field.replace('_', ' ').title()} updated.")


@router.callback_query(F.data == "owner:global_controls")
async def owner_global_controls(callback: CallbackQuery, _: callable, db_session: AsyncSession, **kwargs):
    if not is_owner(callback.from_user.id):
        await callback.answer("Not available.", show_alert=True); return
    groups = await _active_groups(db_session)
    emergency_on = anti_spam_on = anti_flood_on = 0
    for group in groups:
        security = await db_session.scalar(select(SecuritySettings).where(SecuritySettings.group_id == group.id))
        if security:
            emergency_on += int(bool(security.emergency_mode))
            anti_spam_on += int(bool(security.anti_spam))
            anti_flood_on += int(bool(security.anti_flood))
    builder = InlineKeyboardBuilder()
    for label, field, val in [
        ("🟢 Anti-Spam ON — All Groups", "anti_spam", "1"),
        ("🔴 Anti-Spam OFF — All Groups", "anti_spam", "0"),
        ("🟢 Anti-Flood ON — All Groups", "anti_flood", "1"),
        ("🔴 Anti-Flood OFF — All Groups", "anti_flood", "0"),
        ("🚨 Lockdown ON — All Groups", "emergency_mode", "1"),
        ("🔓 Lockdown OFF — All Groups", "emergency_mode", "0"),
    ]:
        builder.button(text=label, callback_data=f"owner:global_set:{field}:{val}")
    builder.button(text="👥 Per-Group Settings", callback_data="owner:groups")
    builder.button(text="◀️ Owner Panel", callback_data="owner:menu")
    builder.adjust(1)
    await callback.message.edit_text(
        "🌐 <b>GLOBAL CONTROLS</b>\n\n"
        f"Active groups: <b>{format_number(len(groups))}</b>\n"
        f"Anti-Spam enabled: <b>{format_number(anti_spam_on)}</b> groups\n"
        f"Anti-Flood enabled: <b>{format_number(anti_flood_on)}</b> groups\n"
        f"Emergency Lockdown enabled: <b>{format_number(emergency_on)}</b> groups\n\n"
        "⚠️ Changes below apply to every active registered group. Lockdown requires Delete Messages permission.",
        parse_mode="HTML", reply_markup=builder.as_markup()
    )
    await callback.answer()


@router.callback_query(F.data.startswith("owner:global_set:"))
async def owner_global_set(callback: CallbackQuery, _: callable, db_session: AsyncSession, **kwargs):
    if not is_owner(callback.from_user.id):
        await callback.answer("Not available.", show_alert=True); return
    try:
        _, _, field, value_text = callback.data.split(":", 3)
        enabled = value_text == "1"
    except ValueError:
        await callback.answer("Invalid global setting.", show_alert=True); return
    if field not in {"anti_spam", "anti_flood", "emergency_mode"} or value_text not in {"0", "1"}:
        await callback.answer("Invalid global setting.", show_alert=True); return
    groups = await _active_groups(db_session); changed = 0
    try:
        for group in groups:
            security = await db_session.scalar(select(SecuritySettings).where(SecuritySettings.group_id == group.id))
            if security is None:
                security = SecuritySettings(group_id=group.id); db_session.add(security)
            setattr(security, field, enabled); changed += 1
        await db_session.commit()
    except Exception as exc:
        await db_session.rollback()
        logger.warning("owner_global_setting_failed", field=field, error=str(exc))
        await callback.answer("Could not apply global setting.", show_alert=True); return
    # Refresh the global dashboard; it will acknowledge this callback once.
    await owner_global_controls(callback, _, db_session)



def _known_bans_kb(group_id: int, members):
    builder = InlineKeyboardBuilder()
    for member in members:
        user = member.user
        telegram_id = getattr(user, "telegram_id", None) or user.id
        name = " ".join(part for part in [user.first_name, user.last_name] if part).strip() or "Unknown user"
        username = f" (@{user.username})" if user.username else ""
        builder.button(
            text=f"🔓 {name[:24]}{username[:12]}",
            callback_data=f"owner:unban:{group_id}:{telegram_id}",
        )
    builder.button(text="🆔 Unban by User ID", callback_data=f"owner:unbanid:{group_id}")
    builder.button(text="🔄 Refresh List", callback_data=f"owner:banlist:{group_id}")
    builder.button(text="◀️ Group Dashboard", callback_data=f"owner:group:{group_id}")
    builder.adjust(1)
    return builder.as_markup()


@router.callback_query(F.data.startswith("owner:banlist:"))
async def owner_known_bans(callback: CallbackQuery, db_session: AsyncSession, **kwargs):
    if not is_owner(callback.from_user.id):
        await callback.answer("Not available.", show_alert=True); return
    try: group_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Invalid group.", show_alert=True); return
    group = await db_session.get(Group, group_id)
    if not group or not group.is_active:
        await callback.answer("Group is not active/registered.", show_alert=True); return
    try:
        me = await callback.bot.get_chat_member(group_id, callback.bot.id)
        status = str(getattr(me.status, "value", me.status))
        if status not in {"administrator", "creator"} or (status == "administrator" and not getattr(me, "can_restrict_members", False)):
            await callback.answer("ALEXA needs admin + Restrict Members permission in this group.", show_alert=True); return
    except Exception:
        await callback.answer("Could not verify ALEXA's permissions in this group.", show_alert=True); return
    result = await db_session.execute(
        select(GroupMember).where(
            GroupMember.group_id == group_id,
            GroupMember.is_banned.is_(True),
        ).order_by(desc(GroupMember.last_activity)).limit(30)
    )
    members = result.scalars().all()
    if not members:
        text = (
            f"🔓 <b>Known Bans — {escape(group.title)}</b>\n\n"
            "No members marked as banned in ALEXA's database. Telegram does not provide bots a complete list of every banned member, "
            "so use <b>Unban by User ID</b> for bans not recorded here."
        )
    else:
        text = (
            f"🔓 <b>Known Bans — {escape(group.title)}</b>\n\n"
            f"Showing up to {len(members)} member(s) recorded as banned by ALEXA. Tap a member to unban.\n"
            "For bans not in this database, use Unban by User ID."
        )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=_known_bans_kb(group_id, members))
    await callback.answer()


@router.callback_query(F.data.startswith("owner:unbanid:"))
async def owner_unban_by_id_start(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession, **kwargs):
    if not is_owner(callback.from_user.id):
        await callback.answer("Not available.", show_alert=True); return
    try: group_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Invalid group.", show_alert=True); return
    group = await db_session.get(Group, group_id)
    if not group or not group.is_active:
        await callback.answer("Group is not active/registered.", show_alert=True); return
    try:
        me = await callback.bot.get_chat_member(group_id, callback.bot.id)
        status = str(getattr(me.status, "value", me.status))
        if status not in {"administrator", "creator"} or (status == "administrator" and not getattr(me, "can_restrict_members", False)):
            await callback.answer("ALEXA needs admin + Restrict Members permission.", show_alert=True); return
    except Exception:
        await callback.answer("Could not verify ALEXA's permissions.", show_alert=True); return
    await state.set_state(OwnerStates.waiting_unban_user_id)
    await state.update_data(unban_group_id=group_id)
    await callback.message.answer(
        f"🆔 <b>Unban Member — {escape(group.title)}</b>\n\n"
        "Send the member's numeric Telegram User ID. Username alone cannot be used by Telegram's unban API.\n"
        "Send /cancel to cancel.",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(OwnerStates.waiting_unban_user_id)
async def owner_unban_user_id_input(message: Message, state: FSMContext, db_session: AsyncSession, **kwargs):
    if not is_owner(message.from_user.id) or message.chat.type != "private":
        await state.clear(); return
    value = (message.text or "").strip()
    if value.lower() == "/cancel":
        await state.clear(); await message.answer("Unban cancelled."); return
    try:
        user_id = int(value)
        if user_id <= 0: raise ValueError
    except ValueError:
        await message.answer("Please send a valid numeric Telegram User ID, or /cancel.")
        return
    data = await state.get_data()
    try: group_id = int(data["unban_group_id"])
    except (KeyError, TypeError, ValueError):
        await state.clear(); await message.answer("Group selection expired. Open Owner Panel again."); return
    group = await db_session.get(Group, group_id)
    if not group or not group.is_active:
        await state.clear(); await message.answer("Group is not active/registered."); return
    try:
        me = await message.bot.get_chat_member(group_id, message.bot.id)
        status = str(getattr(me.status, "value", me.status))
        if status not in {"administrator", "creator"} or (status == "administrator" and not getattr(me, "can_restrict_members", False)):
            await state.clear(); await message.answer("ALEXA needs admin + Restrict Members permission in that group."); return
        await message.bot.unban_chat_member(chat_id=group_id, user_id=user_id, only_if_banned=True)
        record = await db_session.scalar(
            select(GroupMember)
            .join(User, GroupMember.user_id == User.id)
            .where(
                GroupMember.group_id == group_id,
                (User.telegram_id == user_id) | (User.id == user_id),
            )
        )
        if record:
            record.is_banned = False
            record.ban_reason = None
        await db_session.commit()
        await message.answer(f"✅ User <code>{user_id}</code> unbanned from <b>{escape(group.title)}</b>.", parse_mode="HTML")
    except TelegramBadRequest as exc:
        await message.answer(f"Telegram could not unban this ID: {escape(str(exc)[:250])}", parse_mode="HTML")
    except TelegramForbiddenError:
        await message.answer("Unban failed: ALEXA does not have permission in that group.")
    except Exception as exc:
        await db_session.rollback()
        logger.warning("owner_unban_by_id_failed", group_id=group_id, user_id=user_id, error=str(exc))
        await message.answer("Unban failed. Check the group permissions and user ID.")
    finally:
        await state.clear()


@router.callback_query(F.data.startswith("owner:unmuteid:"))
async def owner_unmute_by_id_start(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession, **kwargs):
    if not is_owner(callback.from_user.id):
        await callback.answer("Not available.", show_alert=True)
        return
    try:
        group_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Invalid group.", show_alert=True)
        return
    group = await db_session.get(Group, group_id)
    if not group or not group.is_active:
        await callback.answer("Group is not active/registered.", show_alert=True)
        return
    try:
        me = await callback.bot.get_chat_member(group_id, callback.bot.id)
        status = str(getattr(me.status, "value", me.status))
        if status not in {"administrator", "creator"} or (
            status == "administrator" and not getattr(me, "can_restrict_members", False)
        ):
            await callback.answer("ALEXA needs admin + Restrict Members permission.", show_alert=True)
            return
    except Exception:
        await callback.answer("Could not verify ALEXA's permissions.", show_alert=True)
        return

    await state.set_state(OwnerStates.waiting_unmute_user_id)
    await state.update_data(unmute_group_id=group_id)
    await callback.message.answer(
        f"🔇 <b>Unmute Member — {escape(group.title)}</b>\n\n"
        "Send the member's numeric Telegram User ID. Username alone cannot be used for this action.\n"
        "Send /cancel to cancel.",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(OwnerStates.waiting_unmute_user_id)
async def owner_unmute_user_id_input(message: Message, state: FSMContext, db_session: AsyncSession, **kwargs):
    if not message.from_user or not is_owner(message.from_user.id) or message.chat.type != "private":
        await state.clear()
        return
    value = (message.text or "").strip()
    if value.lower() == "/cancel":
        await state.clear()
        await message.answer("Unmute cancelled.")
        return
    try:
        user_id = int(value)
        if user_id <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Please send a valid numeric Telegram User ID, or /cancel.")
        return

    data = await state.get_data()
    try:
        group_id = int(data["unmute_group_id"])
    except (KeyError, TypeError, ValueError):
        await state.clear()
        await message.answer("Group selection expired. Open Owner Panel again.")
        return
    group = await db_session.get(Group, group_id)
    if not group or not group.is_active:
        await state.clear()
        await message.answer("Group is not active/registered.")
        return

    try:
        me = await message.bot.get_chat_member(group_id, message.bot.id)
        status = str(getattr(me.status, "value", me.status))
        if status not in {"administrator", "creator"} or (
            status == "administrator" and not getattr(me, "can_restrict_members", False)
        ):
            await state.clear()
            await message.answer("ALEXA needs admin + Restrict Members permission in that group.")
            return

        # Reuse the same explicit send permissions as the regular /unmute command.
        from bot.services.moderation_service import FULL_PERMISSIONS
        await message.bot.restrict_chat_member(
            chat_id=group_id,
            user_id=user_id,
            permissions=FULL_PERMISSIONS,
        )
        record = await db_session.scalar(
            select(GroupMember)
            .join(User, GroupMember.user_id == User.id)
            .where(
                GroupMember.group_id == group_id,
                (User.telegram_id == user_id) | (User.id == user_id),
            )
        )
        if record:
            record.is_muted = False
            record.mute_until = None
        await db_session.commit()
        await message.answer(
            f"✅ User <code>{user_id}</code> unmuted in <b>{escape(group.title)}</b>.",
            parse_mode="HTML",
        )
    except TelegramBadRequest as exc:
        await db_session.rollback()
        await message.answer(
            f"Telegram could not unmute this ID: {escape(str(exc)[:250])}",
            parse_mode="HTML",
        )
    except TelegramForbiddenError:
        await db_session.rollback()
        await message.answer("Unmute failed: ALEXA does not have Restrict Members permission.")
    except Exception as exc:
        await db_session.rollback()
        logger.warning("owner_unmute_by_id_failed", group_id=group_id, user_id=user_id, error=str(exc))
        await message.answer("Unmute failed. Check the group permissions and user ID.")
    finally:
        await state.clear()


@router.callback_query(F.data.startswith("owner:unban:"))
async def owner_unban_known_member(callback: CallbackQuery, db_session: AsyncSession, **kwargs):
    if not is_owner(callback.from_user.id):
        await callback.answer("Not available.", show_alert=True); return
    try:
        _, _, group_id_text, user_id_text = callback.data.split(":", 3)
        group_id, user_id = int(group_id_text), int(user_id_text)
    except ValueError:
        await callback.answer("Invalid member.", show_alert=True); return
    group = await db_session.get(Group, group_id)
    if not group or not group.is_active:
        await callback.answer("Group is not active/registered.", show_alert=True); return
    try:
        me = await callback.bot.get_chat_member(group_id, callback.bot.id)
        status = str(getattr(me.status, "value", me.status))
        if status not in {"administrator", "creator"} or (status == "administrator" and not getattr(me, "can_restrict_members", False)):
            await callback.answer("ALEXA needs admin + Restrict Members permission.", show_alert=True); return
        await callback.bot.unban_chat_member(chat_id=group_id, user_id=user_id, only_if_banned=True)
        record = await db_session.scalar(
            select(GroupMember)
            .join(User, GroupMember.user_id == User.id)
            .where(
                GroupMember.group_id == group_id,
                (User.telegram_id == user_id) | (User.id == user_id),
            )
        )
        if record:
            record.is_banned = False
            record.ban_reason = None
        await db_session.commit()
        await callback.answer("Member unbanned successfully.", show_alert=True)
        result = await db_session.execute(
            select(GroupMember).where(
                GroupMember.group_id == group_id,
                GroupMember.is_banned.is_(True),
            ).order_by(desc(GroupMember.last_activity)).limit(30)
        )
        members = result.scalars().all()
        await callback.message.edit_text(
            f"🔓 <b>Known Bans — {escape(group.title)}</b>\n\n"
            + (
                f"Showing up to {len(members)} member(s) recorded as banned by ALEXA.\n"
                "For bans not in this database, use Unban by User ID."
                if members else
                "No members marked as banned in ALEXA's database. Use Unban by User ID for other bans."
            ),
            parse_mode="HTML",
            reply_markup=_known_bans_kb(group_id, members),
        )
    except TelegramBadRequest as exc:
        await callback.answer(f"Telegram unban failed: {str(exc)[:180]}", show_alert=True)
    except TelegramForbiddenError:
        await callback.answer("ALEXA lacks permission to unban this member.", show_alert=True)
    except Exception as exc:
        await db_session.rollback()
        logger.warning("owner_unban_known_member_failed", group_id=group_id, user_id=user_id, error=str(exc))
        await callback.answer("Unban failed. Check permissions.", show_alert=True)



@router.callback_query(F.data.startswith("owner:silent_service:"))
async def owner_toggle_silent_service_messages(callback: CallbackQuery, db_session: AsyncSession, **kwargs):
    if not is_owner(callback.from_user.id):
        await callback.answer("Not available.", show_alert=True)
        return
    try:
        group_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Invalid group.", show_alert=True)
        return
    group = await db_session.get(Group, group_id)
    if not group or not group.is_active:
        await callback.answer("Group is not active/registered.", show_alert=True)
        return
    settings_row = await db_session.scalar(
        select(GroupSettings).where(GroupSettings.group_id == group_id)
    )
    if not settings_row:
        settings_row = GroupSettings(group_id=group_id, silent_service_messages=True)
        db_session.add(settings_row)
        enabled = True
    else:
        settings_row.silent_service_messages = not bool(settings_row.silent_service_messages)
        enabled = bool(settings_row.silent_service_messages)
    await db_session.commit()
    try:
        await _render_group(callback, db_session, group_id)
    except Exception as exc:
        logger.warning("owner_silent_service_refresh_failed", group_id=group_id, error=str(exc))
    await callback.answer(
        "ON: join/leave/ban service messages will be deleted." if enabled
        else "OFF: join/leave/ban service messages are allowed again."
    )


@router.callback_query(F.data.startswith("owner:purge:"))
async def owner_purge_start(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession, **kwargs):
    if not is_owner(callback.from_user.id):
        await callback.answer("Not available.", show_alert=True)
        return
    try:
        group_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Invalid group.", show_alert=True)
        return
    group = await db_session.get(Group, group_id)
    if not group or not group.is_active:
        await callback.answer("Group is not active/registered.", show_alert=True)
        return
    try:
        me = await callback.bot.get_chat_member(group_id, callback.bot.id)
        status = str(getattr(me.status, "value", me.status))
        if status not in {"administrator", "creator"} or (
            status == "administrator" and not getattr(me, "can_delete_messages", False)
        ):
            await callback.answer("ALEXA needs admin + Delete Messages permission in this group.", show_alert=True)
            return
    except Exception:
        await callback.answer("Could not verify ALEXA's permissions in this group.", show_alert=True)
        return

    builder = InlineKeyboardBuilder()
    builder.button(text="🗑️ Purge Recent (up to 1000 IDs)", callback_data=f"owner:purge_recent:{group_id}")
    builder.button(text="🔢 Purge by Message ID Range", callback_data=f"owner:purge_range:{group_id}")
    builder.button(text="◀️ Back to Group", callback_data=f"owner:group:{group_id}")
    builder.adjust(1)
    await callback.message.edit_text(
        f"🧹 <b>Purge Messages — {escape(group.title)}</b>\n\n"
        "Choose how to purge this group.\n\n"
        "• <b>Purge Recent:</b> attempts the latest up to 1000 message IDs.\n"
        "• <b>ID Range:</b> enter a START-END range yourself.\n\n"
        "Telegram does not let bots fetch the entire chat history, and messages older than 48 hours "
        "generally cannot be deleted.",
        parse_mode="HTML",
        reply_markup=builder.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("owner:purge_range:"))
async def owner_purge_range_start(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession, **kwargs):
    if not is_owner(callback.from_user.id):
        await callback.answer("Not available.", show_alert=True)
        return
    try:
        group_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Invalid group.", show_alert=True)
        return
    group = await db_session.get(Group, group_id)
    if not group or not group.is_active:
        await callback.answer("Group is not active/registered.", show_alert=True)
        return
    await state.set_state(OwnerStates.waiting_purge_range)
    await state.update_data(purge_group_id=group_id, purge_group_title=group.title)
    await callback.message.answer(
        f"🧹 <b>Purge Messages — {escape(group.title)}</b>\n\n"
        "Send the message ID range as <code>START-END</code>, for example <code>1200-1250</code>. "
        "Maximum 1000 IDs per request. Telegram may reject messages older than 48 hours. "
        "Send /cancel to stop.",
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("owner:purge_recent:"))
async def owner_purge_recent_start(callback: CallbackQuery, db_session: AsyncSession, **kwargs):
    if not is_owner(callback.from_user.id):
        await callback.answer("Not available.", show_alert=True)
        return
    try:
        group_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Invalid group.", show_alert=True)
        return
    group = await db_session.get(Group, group_id)
    if not group or not group.is_active:
        await callback.answer("Group is not active/registered.", show_alert=True)
        return
    try:
        me = await callback.bot.get_chat_member(group_id, callback.bot.id)
        status = str(getattr(me.status, "value", me.status))
        if status not in {"administrator", "creator"} or (
            status == "administrator" and not getattr(me, "can_delete_messages", False)
        ):
            await callback.answer("ALEXA needs admin + Delete Messages permission in this group.", show_alert=True)
            return
    except Exception:
        await callback.answer("Could not verify ALEXA's permissions in this group.", show_alert=True)
        return
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Yes, purge recent messages", callback_data=f"owner:purge_recent_confirm:{group_id}")
    builder.button(text="❌ Cancel", callback_data=f"owner:purge:{group_id}")
    builder.adjust(1)
    await callback.message.edit_text(
        f"⚠️ <b>Confirm Purge — {escape(group.title)}</b>\n\n"
        "ALEXA will attempt to delete the latest up to 1000 message IDs in this group, including commands. "
        "Some IDs may be empty or undeletable; Telegram usually rejects messages older than 48 hours.\n\n"
        "Continue?",
        parse_mode="HTML",
        reply_markup=builder.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("owner:purge_recent_confirm:"))
async def owner_purge_recent_confirm(callback: CallbackQuery, db_session: AsyncSession, **kwargs):
    if not is_owner(callback.from_user.id):
        await callback.answer("Not available.", show_alert=True)
        return
    try:
        group_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Invalid group.", show_alert=True)
        return
    group = await db_session.get(Group, group_id)
    if not group or not group.is_active:
        await callback.answer("Group is not active/registered.", show_alert=True)
        return
    try:
        me = await callback.bot.get_chat_member(group_id, callback.bot.id)
        status = str(getattr(me.status, "value", me.status))
        if status not in {"administrator", "creator"} or (
            status == "administrator" and not getattr(me, "can_delete_messages", False)
        ):
            await callback.answer("ALEXA needs admin + Delete Messages permission in this group.", show_alert=True)
            return
        marker = await callback.bot.send_message(group_id, "🧹 ALEXA purge started…")
        end_id = marker.message_id
        start_id = max(1, end_id - 999)
        deleted = failed = 0
        for batch_start in range(start_id, end_id + 1, 100):
            batch = list(range(batch_start, min(batch_start + 100, end_id + 1)))
            try:
                await callback.bot.delete_messages(chat_id=group_id, message_ids=batch)
                deleted += len(batch)
            except Exception:
                for message_id in batch:
                    try:
                        await callback.bot.delete_message(chat_id=group_id, message_id=message_id)
                        deleted += 1
                    except Exception:
                        failed += 1
        result_kb = InlineKeyboardBuilder()
        result_kb.button(text="◀️ Back to Group", callback_data=f"owner:group:{group_id}")
        result_kb.button(text="👑 Owner Panel", callback_data="owner:menu")
        result_kb.adjust(1)
        await callback.message.edit_text(
            f"🧹 <b>Purge finished</b>\n\nGroup: <b>{escape(group.title)}</b>\n"
            f"Attempted IDs: <code>{start_id}-{end_id}</code>\n"
            f"Deleted: <b>{deleted}</b>\nCould not delete: <b>{failed}</b>\n\n"
            "Note: this covers recent IDs only; Telegram bots cannot retrieve all historical chat messages.",
            parse_mode="HTML",
            reply_markup=result_kb.as_markup(),
        )
        await callback.answer("Purge finished.")
    except Exception as exc:
        logger.warning("owner_purge_recent_failed", group_id=group_id, error=str(exc))
        await callback.answer("Purge failed. Check bot permissions.", show_alert=True)


@router.message(OwnerStates.waiting_purge_range)
async def owner_purge_range_input(message: Message, state: FSMContext, db_session: AsyncSession, **kwargs):
    if not is_owner(message.from_user.id) or message.chat.type != "private":
        await state.clear(); return
    value = (message.text or "").strip()
    if value.lower() == "/cancel":
        await state.clear(); await message.answer("Purge cancelled."); return
    try:
        left, right = value.replace(" ", "").split("-", 1)
        start_id, end_id = int(left), int(right)
        if start_id <= 0 or end_id < start_id or end_id - start_id + 1 > 1000:
            raise ValueError
    except ValueError:
        await message.answer("Invalid range. Send START-END, e.g. 1200-1250 (maximum 1000 IDs), or /cancel.")
        return
    data = await state.get_data()
    try: group_id = int(data["purge_group_id"])
    except (KeyError, TypeError, ValueError):
        await state.clear(); await message.answer("Group selection expired. Open Owner Panel again."); return
    group = await db_session.get(Group, group_id)
    if not group or not group.is_active:
        await state.clear(); await message.answer("Group is not active/registered."); return
    try:
        me = await message.bot.get_chat_member(group_id, message.bot.id)
        status = str(getattr(me.status, "value", me.status))
        if status not in {"administrator", "creator"} or (status == "administrator" and not getattr(me, "can_delete_messages", False)):
            await state.clear(); await message.answer("ALEXA needs admin + Delete Messages permission in that group."); return
        deleted = failed = 0
        ids = list(range(start_id, end_id + 1))
        for offset in range(0, len(ids), 100):
            batch = ids[offset:offset + 100]
            try:
                await message.bot.delete_messages(chat_id=group_id, message_ids=batch)
                deleted += len(batch)
            except Exception:
                for message_id in batch:
                    try:
                        await message.bot.delete_message(chat_id=group_id, message_id=message_id)
                        deleted += 1
                    except Exception:
                        failed += 1
        await message.answer(
            f"🧹 <b>Purge finished</b>\n\nGroup: <b>{escape(group.title)}</b>\n"
            f"Range: <code>{start_id}-{end_id}</code>\n"
            f"Deleted: <b>{deleted}</b>\nCould not delete: <b>{failed}</b>",
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.warning("owner_purge_range_failed", group_id=group_id, error=str(exc))
        await message.answer("Purge failed. Check the group permissions and message ID range.")
    finally:
        await state.clear()


@router.callback_query(F.data == "owner:banall")
async def owner_banall_groups(callback: CallbackQuery, _: callable, db_session: AsyncSession, **kwargs):
    if not is_owner(callback.from_user.id):
        await callback.answer("Not available.", show_alert=True)
        return
    groups = await _active_groups(db_session)
    await callback.message.edit_text(
        "🔨 <b>BAN ALL</b>\n\nSelect the group. A confirmation step is required before any bans.",
        parse_mode="HTML",
        reply_markup=_group_list_kb(groups, "banall"),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("owner:banall:"))
async def owner_banall_confirm(callback: CallbackQuery, _: callable, db_session: AsyncSession, **kwargs):
    if not is_owner(callback.from_user.id):
        await callback.answer("Not available.", show_alert=True)
        return
    try:
        group_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Invalid group.", show_alert=True)
        return
    group = await db_session.get(Group, group_id)
    if not group or not group.is_active:
        await callback.answer("Group is not active.", show_alert=True)
        return
    tracked = await db_session.scalar(
        select(func.count()).select_from(GroupMember).where(
            GroupMember.group_id == group_id, GroupMember.is_banned.is_(False)
        )
    )
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ CONFIRM BAN ALL", callback_data=f"owner:banall_confirm:{group_id}")
    builder.button(text="❌ CANCEL", callback_data=f"owner:group:{group_id}")
    builder.adjust(1)
    await callback.message.edit_text(
        f"⚠️ <b>BAN ALL CONFIRMATION</b>\n\n"
        f"Group: <b>{escape(group.title)}</b>\n"
        f"Tracked eligible records: <b>{format_number(tracked or 0)}</b>\n\n"
        f"The configured bot owner, Telegram administrators/creator, and the bot itself will be skipped. "
        f"Only members that can be legally restricted by the bot will be attempted.",
        parse_mode="HTML",
        reply_markup=builder.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("owner:banall_confirm:"))
async def owner_banall_execute(callback: CallbackQuery, _: callable, db_session: AsyncSession, bot: Bot, **kwargs):
    if not is_owner(callback.from_user.id):
        await callback.answer("Not available.", show_alert=True)
        return
    try:
        group_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Invalid group.", show_alert=True)
        return
    group = await db_session.get(Group, group_id)
    if not group or not group.is_active:
        await callback.answer("Group is not active.", show_alert=True)
        return

    members_result = await db_session.execute(
        select(GroupMember).where(
            GroupMember.group_id == group_id,
            GroupMember.is_banned.is_(False),
        )
    )
    members = members_result.scalars().all()

    await callback.answer("Ban process started.")
    await callback.message.edit_text(
        f"🔨 <b>BAN ALL STARTED</b>\n\n"
        f"Group: <b>{escape(group.title)}</b>\n"
        f"Tracked records: <b>{format_number(len(members))}</b>\n\n"
        f"Processing silently in the background…",
        parse_mode="HTML",
    )

    banned = 0
    skipped = 0
    failed = 0

    # No group message is sent for individual bans. Telegram/API errors are isolated per user.
    for member in members:
        try:
            # The configured bot owner must never be included in Ban All.
            if member.user_id == settings.effective_owner_id:
                skipped += 1
                continue
            tg_member = await bot.get_chat_member(group_id, member.user_id)
            if tg_member.status in ("creator", "administrator", "left", "kicked") or tg_member.user.is_bot:
                skipped += 1
                continue
            await bot.ban_chat_member(
                chat_id=group_id,
                user_id=member.user_id,
                revoke_messages=True,
            )
            member.is_banned = True
            banned += 1
        except Exception as exc:
            failed += 1
            logger.warning(
                "owner_ban_all_member_failed",
                group_id=group_id,
                user_id=member.user_id,
                error=str(exc),
            )

    await db_session.commit()
    await callback.message.edit_text(
        f"✅ <b>BAN ALL FINISHED</b>\n\n"
        f"Group: <b>{escape(group.title)}</b>\n"
        f"🔨 Banned: <b>{banned}</b>\n"
        f"⏭️ Skipped: <b>{skipped}</b>\n"
        f"❌ Failed: <b>{failed}</b>",
        parse_mode="HTML",
        reply_markup=_group_detail_kb(group_id),
    )


@router.callback_query(F.data == "owner:activities")
async def owner_activities_groups(callback: CallbackQuery, _: callable, db_session: AsyncSession, **kwargs):
    if not is_owner(callback.from_user.id):
        await callback.answer("Not available.", show_alert=True)
        return
    groups = await _active_groups(db_session)
    await callback.message.edit_text(
        "📈 <b>Group Activities</b>\n\nSelect a group.",
        parse_mode="HTML",
        reply_markup=_group_list_kb(groups, "activity"),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("owner:activity:"))
async def owner_activity(callback: CallbackQuery, _: callable, db_session: AsyncSession, **kwargs):
    if not is_owner(callback.from_user.id):
        await callback.answer("Not available.", show_alert=True)
        return
    group_id = int(callback.data.rsplit(":", 1)[1])
    group = await db_session.get(Group, group_id)
    if not group:
        await callback.answer("Group not found.", show_alert=True)
        return
    member_count = await db_session.scalar(
        select(func.count()).select_from(GroupMember).where(GroupMember.group_id == group_id)
    )
    log_count = await db_session.scalar(
        select(func.count()).select_from(ActionLog).where(ActionLog.group_id == group_id)
    )
    logs = (
        await db_session.execute(
            select(ActionLog).where(ActionLog.group_id == group_id)
            .order_by(desc(ActionLog.created_at)).limit(10)
        )
    ).scalars().all()
    text = (
        f"📈 <b>{escape(group.title)} — Activities</b>\n\n"
        f"👥 Tracked members: <b>{format_number(member_count or 0)}</b>\n"
        f"📝 Logged actions: <b>{format_number(log_count or 0)}</b>\n\n"
        f"<b>Recent:</b>\n"
    )
    if logs:
        for log in logs:
            when = log.created_at.strftime("%Y-%m-%d %H:%M") if log.created_at else "unknown"
            text += f"• {escape(log.action)} — {escape(when)}\n"
    else:
        text += "• No activity logs yet.\n"
    await callback.message.edit_text(
        text, parse_mode="HTML", reply_markup=_group_detail_kb(group_id)
    )
    await callback.answer()


@router.callback_query(F.data == "owner:links")
async def owner_links_groups(callback: CallbackQuery, _: callable, db_session: AsyncSession, **kwargs):
    if not is_owner(callback.from_user.id):
        await callback.answer("Not available.", show_alert=True)
        return
    groups = await _active_groups(db_session)
    await callback.message.edit_text(
        "🔗 <b>Group Links</b>\n\nSelect a group.",
        parse_mode="HTML",
        reply_markup=_group_list_kb(groups, "link"),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("owner:link:"))
async def owner_link(callback: CallbackQuery, _: callable, db_session: AsyncSession, bot: Bot, **kwargs):
    if not is_owner(callback.from_user.id):
        await callback.answer("Not available.", show_alert=True)
        return
    group_id = int(callback.data.rsplit(":", 1)[1])
    group = await db_session.get(Group, group_id)
    if not group:
        await callback.answer("Group not found.", show_alert=True)
        return
    try:
        chat = await bot.get_chat(group_id)
        link = chat.invite_link
        if not link and group.username:
            link = f"https://t.me/{group.username.lstrip('@')}"
        if not link:
            invite = await bot.create_chat_invite_link(group_id, name="ALEXA Owner Link")
            link = invite.invite_link
        group.invite_link = link
        text = f"🔗 <b>{escape(group.title)}</b>\n\n{escape(link)}"
        await callback.message.edit_text(
            text, parse_mode="HTML", reply_markup=_group_detail_kb(group_id)
        )
    except Exception as exc:
        logger.warning("owner_group_link_failed", group_id=group_id, error=str(exc))
        await callback.answer(
            "Could not get/create an invite link. Check the bot's invite permission.",
            show_alert=True,
        )
        return
    await callback.answer()


@router.callback_query(F.data == "owner:addgroups")
async def owner_addgroups(callback: CallbackQuery, _: callable, bot: Bot, **kwargs):
    if not is_owner(callback.from_user.id):
        await callback.answer("Not available.", show_alert=True)
        return
    me = await bot.get_me()
    add_link = f"https://t.me/{me.username}?startgroup=true"
    text = (
        "➕ <b>Add ALEXA to Groups</b>\n\n"
        "Use this link to add the bot to a group:\n"
        f"{escape(add_link)}\n\n"
        "After adding ALEXA as an administrator, the group will appear in Active Groups "
        "when Telegram sends the membership update."
    )
    await callback.message.edit_text(
        text, parse_mode="HTML", reply_markup=back_button_kb(_, "owner:menu")
    )
    await callback.answer()


@router.callback_query(F.data == "owner:users")
@router.callback_query(F.data.startswith("owner:users:"))
async def owner_users(callback: CallbackQuery, _: callable, db_session: AsyncSession, **kwargs):
    if not is_owner(callback.from_user.id):
        await callback.answer("Not available.", show_alert=True)
        return

    # Show every recorded DM starter through pages so the full list remains
    # usable even when the database contains hundreds or thousands of users.
    page = 0
    if callback.data.startswith("owner:users:"):
        try:
            page = max(0, int(callback.data.rsplit(":", 1)[1]))
        except (TypeError, ValueError):
            page = 0
    page_size = 25
    user_filter = (User.dm_started.is_(True), User.is_banned.is_(False))
    total = await db_session.scalar(
        select(func.count()).select_from(User).where(*user_filter)
    ) or 0
    max_page = max(0, (total - 1) // page_size)
    page = min(page, max_page)
    result = await db_session.execute(
        select(User).where(*user_filter)
        .order_by(desc(User.last_seen), desc(User.created_at))
        .offset(page * page_size).limit(page_size)
    )
    users = result.scalars().all()

    if not users:
        body = "No active DM starters have been recorded yet."
    else:
        lines = []
        for user in users:
            name = escape((user.first_name or "User") + (f" {user.last_name}" if user.last_name else ""))
            username = f" (@{escape(user.username)})" if user.username else ""
            lines.append(f'• <a href="tg://user?id={user.id}">{name}</a>{username} — <code>{user.id}</code>')
        body = "\n".join(lines)

    builder = InlineKeyboardBuilder()
    if page > 0:
        builder.button(text="⬅️ Previous", callback_data=f"owner:users:{page - 1}")
    if page < max_page:
        builder.button(text="Next ➡️", callback_data=f"owner:users:{page + 1}")
    builder.button(text="◀️ Owner Panel", callback_data="owner:menu")
    builder.adjust(2, 1)

    page_label = f"Page {page + 1}/{max_page + 1}" if total else "Page 0/0"
    await callback.message.edit_text(
        "👤 <b>Active DM Users</b>\n"
        f"👥 Total: <b>{format_number(total)}</b>\n"
        f"📄 {page_label} (25 per page)\n\n" + body,
        parse_mode="HTML", reply_markup=builder.as_markup()
    )
    await callback.answer()


@router.callback_query(F.data == "owner:maintenance")
async def toggle_maintenance(callback: CallbackQuery, _: callable, **kwargs):
    global _maintenance_mode
    if not is_owner(callback.from_user.id):
        await callback.answer("Not available.", show_alert=True)
        return
    _maintenance_mode = not _maintenance_mode
    await callback.answer(
        "Maintenance ON." if _maintenance_mode else "Maintenance OFF.",
        show_alert=True,
    )
    await callback.message.edit_reply_markup(reply_markup=owner_kb())


@router.callback_query(F.data == "owner:emergency")
async def emergency_controls(callback: CallbackQuery, _: callable, db_session: AsyncSession, **kwargs):
    if not is_owner(callback.from_user.id):
        await callback.answer("Not available.", show_alert=True)
        return
    groups = await _active_groups(db_session)
    await callback.message.edit_text(
        "🚨 <b>EMERGENCY LOCKDOWN</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Choose a group to activate or disable lockdown.\n\n"
        "🔴 <b>ON</b> — non-admin messages are deleted\n"
        "🟢 <b>OFF</b> — normal group messages\n\n"
        "⚠️ ALEXA needs <b>Delete Messages</b> permission in each group.",
        parse_mode="HTML",
        reply_markup=await _emergency_groups_kb(groups, db_session) if groups else back_button_kb(_, "owner:menu")
    )
    await callback.answer()


@router.callback_query(F.data.startswith("owner:emergency:"))
async def owner_emergency_toggle(callback: CallbackQuery, _: callable, db_session: AsyncSession, **kwargs):
    if not is_owner(callback.from_user.id):
        await callback.answer("Not available.", show_alert=True)
        return
    try:
        group_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Invalid group.", show_alert=True); return
    group = await db_session.get(Group, group_id)
    if not group or not group.is_active:
        await callback.answer("Group is not active.", show_alert=True); return
    security = await db_session.scalar(select(SecuritySettings).where(SecuritySettings.group_id == group_id))
    if not security:
        security = SecuritySettings(group_id=group_id)
        db_session.add(security)
        await db_session.flush()
    security.emergency_mode = not bool(security.emergency_mode)
    await db_session.flush()
    await db_session.commit()
    status = "ON" if security.emergency_mode else "OFF"
    enabled = bool(security.emergency_mode)
    await callback.answer(f"Emergency lockdown {status} for {group.title}.", show_alert=True)
    await callback.message.edit_text(
        f"🚨 <b>EMERGENCY LOCKDOWN</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"👥 Group: <b>{escape(group.title)}</b>\n"
        f"Status: <b>{'🔴 ON' if enabled else '🟢 OFF'}</b>\n\n"
        f"{'⛔ Non-admin messages will be deleted.' if enabled else '✅ Normal messages are allowed.'}\n\n"
        "ALEXA must have <b>Delete Messages</b> permission in this group.",
        parse_mode="HTML", reply_markup=_emergency_detail_kb(group_id, enabled)
    )


@router.callback_query(F.data.startswith("owner:emergency_view:"))
async def owner_emergency_view(callback: CallbackQuery, _: callable, db_session: AsyncSession, **kwargs):
    if not is_owner(callback.from_user.id):
        await callback.answer("Not available.", show_alert=True)
        return
    try:
        group_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Invalid group.", show_alert=True)
        return
    group = await db_session.get(Group, group_id)
    if not group or not group.is_active:
        await callback.answer("Group is not active.", show_alert=True)
        return
    security = await db_session.scalar(select(SecuritySettings).where(SecuritySettings.group_id == group_id))
    enabled = bool(security and security.emergency_mode)
    await callback.message.edit_text(
        f"🚨 <b>EMERGENCY LOCKDOWN</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"👥 Group: <b>{escape(group.title)}</b>\n"
        f"Status: <b>{'🔴 ON' if enabled else '🟢 OFF'}</b>\n\n"
        "ALEXA must have <b>Delete Messages</b> permission in this group.",
        parse_mode="HTML", reply_markup=_emergency_detail_kb(group_id, enabled)
    )
    await callback.answer("Status refreshed.")


@router.callback_query(F.data == "owner:menu")
async def owner_menu_back(callback: CallbackQuery, _: callable, **kwargs):
    if not is_owner(callback.from_user.id):
        await callback.answer("Not available.", show_alert=True)
        return
    await callback.message.edit_text(
        "💠 <b>ALEXA X</b>  <code>CYBER NEON PRO</code>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "👑 <b>OWNER ACCESS: VERIFIED</b>\n"
        "⚡ <b>ULTRA CONTROL DECK</b>\n\n"
        "📡 <b>CONTROL MODULES</b>\n"
        "Broadcast • Promotion • Global Controls • Statistics\n"
        "Groups • Referrals • Ban All • Settings\n"
        "Maintenance • User List • User Messages • Emergency Controls\n\n"
        "🔹 Select a module below. Existing actions and permissions are retained.",
        reply_markup=owner_kb(),
        parse_mode="HTML",
    )
    await callback.answer()


@router.callback_query(F.data == "owner:promotion")
async def start_group_promotion(callback: CallbackQuery, _: callable, state: FSMContext, **kwargs):
    if not is_owner(callback.from_user.id):
        await callback.answer("Not available.", show_alert=True)
        return
    await state.set_state(OwnerStates.waiting_group_promotion)
    await callback.message.answer(
        "📣 Send the announcement/promotion message now. It will be copied to every active group registered by ALEXA.\n\n"
        "Send /cancel to stop. Only the bot owner can use this action."
    )
    await callback.answer()


@router.message(OwnerStates.waiting_group_promotion)
async def process_group_promotion(message: Message, _: callable, db_session: AsyncSession, state: FSMContext, bot: Bot, **kwargs):
    if not is_owner(message.from_user.id) or message.chat.type != "private":
        await state.clear()
        return
    if (message.text or "").strip().lower() == "/cancel":
        await state.clear()
        await message.reply("Group promotion cancelled.")
        return
    groups = await _active_groups(db_session)
    if not groups:
        await state.clear()
        await message.reply("No active groups are registered yet.")
        return
    sent = 0
    failed = []
    for group in groups:
        try:
            await message.copy_to(chat_id=group.id)
            sent += 1
        except TelegramRetryAfter as exc:
            await asyncio.sleep(max(1, exc.retry_after))
            try:
                await message.copy_to(chat_id=group.id)
                sent += 1
            except Exception:
                failed.append(group.title or str(group.id))
        except Exception:
            failed.append(group.title or str(group.id))
        await asyncio.sleep(0.05)
    await state.clear()
    details = "\n".join(f"• {escape(name)}" for name in failed[:20])
    summary = f"📣 <b>Group Promotion Result</b>\n\nSent: <b>{sent}</b>\nFailed: <b>{len(failed)}</b>"
    if details:
        summary += "\n\n<b>Failed groups (first 20):</b>\n" + details
    await message.reply(summary, parse_mode="HTML")


@router.message(OwnerStates.waiting_broadcast)
async def process_broadcast(message: Message, _: callable, db_session: AsyncSession, state: FSMContext, **kwargs):
    if not is_owner(message.from_user.id):
        await state.clear()
        return
    if message.chat.type != "private":
        return
    if (message.text or "").strip().lower() == "/cancel":
        await state.clear(); await message.reply("DM broadcast cancelled."); return
    users = await db_session.execute(
        select(User).where(User.dm_started.is_(True), User.is_banned.is_(False)).order_by(User.id.asc())
    )
    sent = failed = 0
    for user in users.scalars().all():
        try:
            await message.copy_to(user.id); sent += 1
        except TelegramRetryAfter as exc:
            await asyncio.sleep(max(1, exc.retry_after))
            try: await message.copy_to(user.id); sent += 1
            except Exception: failed += 1
        except Exception: failed += 1
        await asyncio.sleep(0.05)
    await state.clear()
    await message.reply(f"📨 <b>DM Broadcast Result</b>\n\nSent: <b>{sent}</b>\nFailed: <b>{failed}</b>", parse_mode="HTML")


@router.callback_query(F.data == "owner:broadcast")
async def start_broadcast(callback: CallbackQuery, _: callable, state: FSMContext, **kwargs):
    if not is_owner(callback.from_user.id):
        await callback.answer("Not available.", show_alert=True)
        return
    await callback.message.answer("📨 <b>DM Broadcast</b>\nSend the message to every recorded user who has started ALEXA in private.\n\nSend /cancel to stop.", parse_mode="HTML")
    await state.set_state(OwnerStates.waiting_broadcast)
    await callback.answer()


def get_maintenance_mode() -> bool:
    return _maintenance_mode


@router.callback_query(F.data == "owner:inbox")
async def owner_inbox(callback: CallbackQuery, db_session: AsyncSession, **kwargs):
    if not is_owner(callback.from_user.id) or not callback.message or callback.message.chat.type != "private":
        await callback.answer("Not available.", show_alert=True)
        return
    rows = (await db_session.scalars(
        select(InboxMessage).order_by(desc(InboxMessage.created_at), desc(InboxMessage.id)).limit(500)
    )).all()
    latest_by_user = {}
    for row in rows:
        latest_by_user.setdefault(row.user_id, row)
    if not latest_by_user:
        await callback.message.edit_text(
            "📩 <b>USER MESSAGES</b>\n\nNo user messages have been received yet.",
            parse_mode="HTML", reply_markup=back_button_kb(None, "owner:menu")
        )
        await callback.answer()
        return
    builder = InlineKeyboardBuilder()
    for user_id in list(latest_by_user)[:40]:
        user = await db_session.get(User, user_id)
        if not user:
            continue
        unread = await db_session.scalar(select(func.count()).select_from(InboxMessage).where(
            InboxMessage.user_id == user_id, InboxMessage.is_read.is_(False)
        ))
        name = (user.first_name or "User")[:24]
        handle = f" @{user.username}" if user.username else ""
        label = f"{'🆕 ' + str(unread) + ' · ' if unread else ''}{name}{handle}"[:60]
        builder.button(text=label, callback_data=f"owner:inbox:{user_id}")
    builder.button(text="🔄 Refresh", callback_data="owner:inbox")
    builder.button(text="◀️ Owner Panel", callback_data="owner:menu")
    builder.adjust(1)
    total_unread = await db_session.scalar(select(func.count()).select_from(InboxMessage).where(InboxMessage.is_read.is_(False)))
    await callback.message.edit_text(
        "📩 <b>USER MESSAGES</b>\n━━━━━━━━━━━━━━━━━━\n"
        f"👤 Conversations: <b>{len(latest_by_user)}</b>\n"
        f"🔔 Unread messages: <b>{total_unread or 0}</b>\n\n"
        "Choose a user to open their saved messages.",
        parse_mode="HTML", reply_markup=builder.as_markup()
    )
    await callback.answer()


@router.callback_query(F.data.startswith("owner:inbox_delete:"))
async def owner_inbox_delete_message(callback: CallbackQuery, db_session: AsyncSession, **kwargs):
    if not is_owner(callback.from_user.id) or not callback.message or callback.message.chat.type != "private":
        await callback.answer("Not available.", show_alert=True)
        return
    try:
        _, _, user_id_text, inbox_id_text = callback.data.split(":")
        user_id, inbox_id = int(user_id_text), int(inbox_id_text)
    except (ValueError, IndexError):
        await callback.answer("Invalid message.", show_alert=True)
        return
    item = await db_session.get(InboxMessage, inbox_id)
    if not item or item.user_id != user_id:
        await callback.answer("Message already removed.", show_alert=True)
        return
    await db_session.delete(item)
    await db_session.commit()
    await _render_inbox_conversation(callback, db_session, user_id)
    await callback.answer("Message removed from Alexa inbox.")


async def _render_inbox_conversation(callback: CallbackQuery, db_session: AsyncSession, user_id: int):
    user = await db_session.get(User, user_id)
    if not user:
        await callback.answer("User not found.", show_alert=True)
        return
    messages = (await db_session.scalars(
        select(InboxMessage).where(InboxMessage.user_id == user_id)
        .order_by(desc(InboxMessage.created_at), desc(InboxMessage.id)).limit(10)
    )).all()
    for item in messages:
        item.is_read = True
    sender_name = escape(user.first_name or "User")
    sender_handle = f"@{escape(user.username)}" if user.username else "No username"
    lines = [
        "📩 <b>USER CHAT</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"👤 <b>{sender_name}</b>",
        f"🔗 {sender_handle}  •  🆔 <code>{user.id}</code>",
        "",
    ]
    if messages:
        for item in reversed(messages):
            body = (item.preview or f"[{item.content_type}]").strip()
            if item.content_type != "text" and not body:
                body = f"[{item.content_type}]"
            stamp = item.created_at.strftime("%d %b %H:%M") if item.created_at else "time unknown"
            lines.append(f"<b>{sender_name}:</b>\n{escape(body[:900])}\n<code>{escape(stamp)}</code>\n")
    else:
        lines.append("No saved messages from this user.")
    lines.extend(["", f"Username: <code>{sender_handle}</code>", f"User ID: <code>{user.id}</code>"])
    builder = InlineKeyboardBuilder()
    for item in reversed(messages):
        builder.button(text=f"↩️ Reply to message #{item.id}", callback_data=f"owner:inbox_reply:{user_id}:{item.id}")
        builder.button(text=f"🗑️ Delete message #{item.id}", callback_data=f"owner:inbox_delete:{user_id}:{item.id}")
    builder.button(text="🔄 Refresh Messages", callback_data=f"owner:inbox:{user_id}")
    builder.button(text="◀️ All User Messages", callback_data="owner:inbox")
    builder.button(text="👑 Owner Panel", callback_data="owner:menu")
    builder.adjust(1)
    await callback.message.edit_text(
        "\n".join(lines)[:3900],
        parse_mode="HTML", reply_markup=builder.as_markup(), disable_web_page_preview=True
    )


@router.callback_query(F.data.startswith("owner:inbox:"))
async def owner_inbox_conversation(callback: CallbackQuery, db_session: AsyncSession, **kwargs):
    if not is_owner(callback.from_user.id) or not callback.message or callback.message.chat.type != "private":
        await callback.answer("Not available.", show_alert=True)
        return
    try:
        user_id = int(callback.data.rsplit(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Invalid user.", show_alert=True)
        return
    await _render_inbox_conversation(callback, db_session, user_id)
    await callback.answer("Messages refreshed.")


@router.callback_query(F.data.startswith("owner:inbox_reply:"))
async def owner_inbox_reply_start(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession, **kwargs):
    if not is_owner(callback.from_user.id) or not callback.message or callback.message.chat.type != "private":
        await callback.answer("Not available.", show_alert=True)
        return
    try:
        _, _, user_id_text, inbox_id_text = callback.data.split(":")
        user_id, inbox_id = int(user_id_text), int(inbox_id_text)
    except (ValueError, IndexError):
        await callback.answer("Invalid message.", show_alert=True)
        return
    item = await db_session.get(InboxMessage, inbox_id)
    if not item or item.user_id != user_id:
        await callback.answer("Message not found.", show_alert=True)
        return
    await state.set_state(OwnerStates.waiting_inbox_reply)
    await state.update_data(inbox_reply_user_id=user_id, inbox_reply_inbox_id=inbox_id)
    await callback.message.answer(
        f"↩️ Replying to message #{inbox_id} from user <code>{user_id}</code>.\n"
        "Send your reply now. It will be attached to that exact message in the user's chat.\n"
        "Send /cancel to stop.", parse_mode="HTML"
    )
    await callback.answer()


@router.message(OwnerStates.waiting_inbox_reply)
async def owner_inbox_reply_send(message: Message, state: FSMContext, db_session: AsyncSession, **kwargs):
    if not is_owner(message.from_user.id) or message.chat.type != "private":
        await state.clear()
        return
    if (message.text or "").strip().lower() == "/cancel":
        await state.clear()
        await message.reply("Reply cancelled.")
        return
    data = await state.get_data()
    try:
        user_id = int(data.get("inbox_reply_user_id"))
        inbox_id = int(data.get("inbox_reply_inbox_id"))
        item = await db_session.get(InboxMessage, inbox_id)
        if not item or item.user_id != user_id:
            await message.reply("❌ This inbox message no longer exists. Open the inbox and choose a message again.")
            return
        await message.copy_to(
            chat_id=user_id,
            reply_parameters=ReplyParameters(message_id=item.telegram_message_id, allow_sending_without_reply=True),
        )
        await message.reply(f"✅ Reply sent to message #{inbox_id} through ALEXA.")
    except TelegramForbiddenError:
        await message.reply("❌ ALEXA cannot message this user. They may have blocked the bot.")
    except Exception as exc:
        logger.warning("Owner inbox reply failed", error=str(exc))
        await message.reply("❌ Could not deliver the reply. Check that the user has started ALEXA.")
    finally:
        await state.clear()



@router.callback_query(F.data.startswith("owner:"))
async def owner_catch_all(callback: CallbackQuery, _: callable, **kwargs):
    if not is_owner(callback.from_user.id):
        await callback.answer("Not available.", show_alert=True)
        return
    await callback.answer("Feature unavailable.", show_alert=True)



