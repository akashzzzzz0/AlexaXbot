import asyncio
from aiogram import Router, F, Bot
from aiogram.types import CallbackQuery, Message, BotCommand, BotCommandScopeDefault, BotCommandScopeChat
from aiogram.exceptions import TelegramRetryAfter
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, desc
from aiogram.utils.keyboard import InlineKeyboardBuilder
from bot.database.models import User, Group, GroupMember, ActionLog, SecuritySettings
from bot.keyboards.main_menu import back_button_kb
from bot.config import settings
from bot.utils.helpers import format_number
import structlog
from html import escape

logger = structlog.get_logger()
router = Router()

_maintenance_mode = False


class OwnerStates(StatesGroup):
    waiting_broadcast = State()
    waiting_group_promotion = State()


def is_owner(user_id: int) -> bool:
    return bool(settings.effective_owner_id) and user_id == settings.effective_owner_id


def owner_kb(_=None) -> object:
    builder = InlineKeyboardBuilder()
    builder.button(text="📢 Global Broadcast", callback_data="owner:broadcast")
    builder.button(text="📣 Group Promotion", callback_data="owner:promotion")
    builder.button(text="📊 Statistics", callback_data="owner:stats")
    builder.button(text="👥 Groups • Activity • Links • Permissions", callback_data="owner:groups")
    builder.button(text="🔨 Ban All", callback_data="owner:banall")
    builder.button(text="⚙️ Settings", callback_data="owner:settings")
    builder.button(text="➕ Add Groups", callback_data="owner:addgroups")
    builder.button(text=f"{'🟢 Disable' if _maintenance_mode else '🔧 Enable'} Maintenance", callback_data="owner:maintenance")
    builder.button(text="👤 User List", callback_data="owner:users")
    builder.button(text="🚨 Emergency Controls", callback_data="owner:emergency")
    builder.adjust(2, 1, 2, 2, 2)
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
    builder.button(text="◀️ Back", callback_data="owner:menu")
    builder.adjust(1)
    return builder.as_markup()


def _group_detail_kb(group_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(text="🔨 Ban All", callback_data=f"owner:banall:{group_id}")
    builder.button(text="🔄 Refresh", callback_data=f"owner:group:{group_id}")
    builder.button(text="◀️ Groups", callback_data="owner:groups")
    builder.adjust(1, 2)
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
        "👑 <b>ALEXA OWNER PANEL</b>\n\nOnly the bot owner can access these controls.",
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
        body = "\\n".join(lines)

    builder = InlineKeyboardBuilder()
    if page > 0:
        builder.button(text="⬅️ Previous", callback_data=f"owner:users:{page - 1}")
    if page < max_page:
        builder.button(text="Next ➡️", callback_data=f"owner:users:{page + 1}")
    builder.button(text="◀️ Owner Panel", callback_data="owner:menu")
    builder.adjust(2, 1)

    page_label = f"Page {page + 1}/{max_page + 1}" if total else "Page 0/0"
    await callback.message.edit_text(
        "👤 <b>Active DM Users</b>\\n"
        f"👥 Total: <b>{format_number(total)}</b>\\n"
        f"📄 {page_label} (25 per page)\\n\\n" + body,
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
        "🚨 <b>Emergency Lockdown</b>\n\nSelect a group to toggle lockdown. When enabled, non-admin group messages are deleted.",
        parse_mode="HTML", reply_markup=_group_list_kb(groups, "emergency") if groups else back_button_kb(_, "owner:menu")
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
    status = "ON" if security.emergency_mode else "OFF"
    await callback.answer(f"Emergency lockdown {status} for {group.title}.", show_alert=True)
    await callback.message.edit_text(
        f"🚨 <b>{escape(group.title)}</b>\nEmergency lockdown: <b>{status}</b>\n\nWhen enabled, non-admin messages are deleted.",
        parse_mode="HTML", reply_markup=_group_detail_kb(group_id)
    )


@router.callback_query(F.data == "owner:menu")
async def owner_menu_back(callback: CallbackQuery, _: callable, **kwargs):
    if not is_owner(callback.from_user.id):
        await callback.answer("Not available.", show_alert=True)
        return
    await callback.message.edit_text(
        "👑 <b>ALEXA OWNER PANEL</b>",
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
    users = await db_session.execute(
        select(User).where(User.dm_started.is_(True), User.is_banned.is_(False)).limit(1000)
    )
    sent = 0
    for user in users.scalars().all():
        try:
            await message.copy_to(user.id)
            sent += 1
        except Exception:
            pass
    await message.reply(f"📢 Broadcast sent to {sent} users.")
    await state.clear()


@router.callback_query(F.data == "owner:broadcast")
async def start_broadcast(callback: CallbackQuery, _: callable, state: FSMContext, **kwargs):
    if not is_owner(callback.from_user.id):
        await callback.answer("Not available.", show_alert=True)
        return
    await callback.message.answer("📢 Send the broadcast message:")
    await state.set_state(OwnerStates.waiting_broadcast)
    await callback.answer()


def get_maintenance_mode() -> bool:
    return _maintenance_mode

@router.callback_query(F.data.startswith("owner:"))
async def owner_catch_all(callback: CallbackQuery, _: callable, **kwargs):
    if not is_owner(callback.from_user.id):
        await callback.answer("Not available.", show_alert=True)
        return
    await callback.answer("Feature unavailable.", show_alert=True)



