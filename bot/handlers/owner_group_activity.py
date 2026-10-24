from html import escape

from aiogram import Router, F
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.database.models import Group, GroupMember, GroupStats, ActionLog
from bot.utils.helpers import format_number

router = Router()


def _is_owner(user_id: int) -> bool:
    return bool(settings.effective_owner_id) and user_id == settings.effective_owner_id


def _back_kb(target: str = "owner:groups"):
    builder = InlineKeyboardBuilder()
    builder.button(text="◀️ Group Activity", callback_data=target)
    builder.button(text="👑 Owner Panel", callback_data="owner:menu")
    builder.adjust(1)
    return builder.as_markup()


async def _active_groups(session: AsyncSession):
    result = await session.scalars(
        select(Group).where(Group.is_active.is_(True)).order_by(Group.title.asc())
    )
    return result.all()


def _group_picker(groups, callback_prefix: str, back_target: str = "owner:groups"):
    builder = InlineKeyboardBuilder()
    for group in groups:
        builder.button(
            text=f"👥 {(group.title or 'Untitled')[:45]}",
            callback_data=f"{callback_prefix}:{group.id}",
        )
    builder.button(text="◀️ Group Activity", callback_data=back_target)
    builder.button(text="👑 Owner Panel", callback_data="owner:menu")
    builder.adjust(1)
    return builder.as_markup()


@router.callback_query(F.data == "owner:groups")
async def group_activity_menu(callback: CallbackQuery):
    if not _is_owner(callback.from_user.id):
        await callback.answer("Not available.", show_alert=True)
        return
    builder = InlineKeyboardBuilder()
    builder.button(text="👥 1. Groups List", callback_data="owner:groups_list")
    builder.button(text="📊 2. Statistics", callback_data="owner:group_stats")
    builder.button(text="🛡️ 3. Security", callback_data="owner:group_security_list")
    builder.button(text="⚙️ 4. Group Actions", callback_data="owner:group_actions_list")
    builder.button(text="📜 5. Activity Logs", callback_data="owner:group_logs")
    builder.button(text="◀️ Owner Panel", callback_data="owner:menu")
    builder.adjust(1)
    await callback.message.edit_text(
        "👥 <b>GROUP ACTIVITY CENTER</b>\n\n"
        "Choose one of the five sections below. Each section works with Alexa's registered groups.\n\n"
        "① Groups List — connected groups, IDs, links and permissions\n"
        "② Statistics — tracked members, messages and moderation counts\n"
        "③ Security — per-group security and content locks\n"
        "④ Group Actions — open a group's existing management controls\n"
        "⑤ Activity Logs — recent recorded actions and outcomes\n\n"
        "Music controls are not included here.",
        parse_mode="HTML",
        reply_markup=builder.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data == "owner:groups_list")
async def group_activity_list(callback: CallbackQuery, db_session: AsyncSession):
    if not _is_owner(callback.from_user.id):
        await callback.answer("Not available.", show_alert=True)
        return
    groups = await _active_groups(db_session)
    if not groups:
        await callback.message.edit_text(
            "👥 <b>Groups List</b>\n\nNo active groups are registered yet.",
            parse_mode="HTML", reply_markup=_back_kb()
        )
    else:
        await callback.message.edit_text(
            f"👥 <b>Groups List</b>\n\nActive registered groups: <b>{len(groups)}</b>\nTap a group to open its dashboard.",
            parse_mode="HTML",
            reply_markup=_group_picker(groups, "owner:group", "owner:groups"),
        )
    await callback.answer()


@router.callback_query(F.data == "owner:group_stats")
async def group_stats_picker(callback: CallbackQuery, db_session: AsyncSession):
    if not _is_owner(callback.from_user.id):
        await callback.answer("Not available.", show_alert=True)
        return
    groups = await _active_groups(db_session)
    if not groups:
        await callback.message.edit_text(
            "📊 <b>Group Statistics</b>\n\nNo active groups are registered.",
            parse_mode="HTML", reply_markup=_back_kb()
        )
    else:
        await callback.message.edit_text(
            "📊 <b>GROUP STATISTICS</b>\n\nChoose a group to view its recorded statistics.",
            parse_mode="HTML",
            reply_markup=_group_picker(groups, "owner:group_stats_detail"),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("owner:group_stats_detail:"))
async def group_stats_detail(callback: CallbackQuery, db_session: AsyncSession):
    if not _is_owner(callback.from_user.id):
        await callback.answer("Not available.", show_alert=True)
        return
    try:
        group_id = int(callback.data.rsplit(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Invalid group.", show_alert=True)
        return
    group = await db_session.get(Group, group_id)
    if not group or not group.is_active:
        await callback.answer("Group is not active/registered.", show_alert=True)
        return

    tracked_members = await db_session.scalar(
        select(func.count()).select_from(GroupMember).where(GroupMember.group_id == group_id)
    ) or 0
    banned_members = await db_session.scalar(
        select(func.count()).select_from(GroupMember).where(
            GroupMember.group_id == group_id, GroupMember.is_banned.is_(True)
        )
    ) or 0
    muted_members = await db_session.scalar(
        select(func.count()).select_from(GroupMember).where(
            GroupMember.group_id == group_id, GroupMember.is_muted.is_(True)
        )
    ) or 0
    logged_actions = await db_session.scalar(
        select(func.count()).select_from(ActionLog).where(ActionLog.group_id == group_id)
    ) or 0
    member_messages = await db_session.scalar(
        select(func.coalesce(func.sum(GroupMember.message_count), 0)).where(
            GroupMember.group_id == group_id
        )
    ) or 0
    stats = await db_session.scalar(select(GroupStats).where(GroupStats.group_id == group_id))
    total_messages = int(getattr(stats, "total_messages", 0) or 0) if stats else int(member_messages)
    total_joins = int(getattr(stats, "total_joins", 0) or 0) if stats else 0
    total_leaves = int(getattr(stats, "total_leaves", 0) or 0) if stats else 0
    total_bans = int(getattr(stats, "total_bans", 0) or 0) if stats else int(banned_members)
    peak_members = int(getattr(stats, "peak_members", 0) or 0) if stats else int(getattr(group, "member_count", 0) or 0)

    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 Refresh", callback_data=f"owner:group_stats_detail:{group_id}")
    builder.button(text="📜 Activity Logs", callback_data=f"owner:group_logs_detail:{group_id}")
    builder.button(text="⚙️ Group Actions", callback_data=f"owner:group:{group_id}")
    builder.button(text="◀️ Statistics List", callback_data="owner:group_stats")
    builder.button(text="◀️ Group Activity", callback_data="owner:groups")
    builder.adjust(2, 1, 2)
    await callback.message.edit_text(
        f"📊 <b>Statistics — {escape(group.title or 'Untitled')}</b>\n\n"
        f"🆔 Group ID: <code>{group.id}</code>\n"
        f"👥 Tracked members: <b>{format_number(tracked_members)}</b>\n"
        f"💬 Recorded messages: <b>{format_number(total_messages)}</b>\n"
        f"🟢 Recorded joins: <b>{format_number(total_joins)}</b>\n"
        f"🚪 Recorded leaves: <b>{format_number(total_leaves)}</b>\n"
        f"🔨 Recorded bans: <b>{format_number(total_bans)}</b>\n"
        f"🔇 Currently tracked muted: <b>{format_number(muted_members)}</b>\n"
        f"🚫 Currently tracked banned: <b>{format_number(banned_members)}</b>\n"
        f"📈 Peak/registered member count: <b>{format_number(peak_members)}</b>\n"
        f"📝 Logged actions: <b>{format_number(logged_actions)}</b>\n\n"
        "Counts depend on the events and member activity Alexa has actually recorded.",
        parse_mode="HTML", reply_markup=builder.as_markup()
    )
    await callback.answer()


@router.callback_query(F.data == "owner:group_security_list")
async def group_security_picker(callback: CallbackQuery, db_session: AsyncSession):
    if not _is_owner(callback.from_user.id):
        await callback.answer("Not available.", show_alert=True)
        return
    groups = await _active_groups(db_session)
    if not groups:
        await callback.message.edit_text(
            "🛡️ <b>Group Security</b>\n\nNo active groups are registered.",
            parse_mode="HTML", reply_markup=_back_kb()
        )
    else:
        await callback.message.edit_text(
            "🛡️ <b>GROUP SECURITY</b>\n\nChoose a group to open its existing security and content-lock controls.",
            parse_mode="HTML",
            reply_markup=_group_picker(groups, "owner:security"),
        )
    await callback.answer()


@router.callback_query(F.data == "owner:group_actions_list")
async def group_actions_picker(callback: CallbackQuery, db_session: AsyncSession):
    if not _is_owner(callback.from_user.id):
        await callback.answer("Not available.", show_alert=True)
        return
    groups = await _active_groups(db_session)
    if not groups:
        await callback.message.edit_text(
            "⚙️ <b>Group Actions</b>\n\nNo active groups are registered.",
            parse_mode="HTML", reply_markup=_back_kb()
        )
    else:
        await callback.message.edit_text(
            "⚙️ <b>GROUP ACTIONS</b>\n\nChoose a group to open its existing dashboard. Destructive actions keep their confirmation steps.",
            parse_mode="HTML",
            reply_markup=_group_picker(groups, "owner:group"),
        )
    await callback.answer()


@router.callback_query(F.data == "owner:group_logs")
async def group_logs_picker(callback: CallbackQuery, db_session: AsyncSession):
    if not _is_owner(callback.from_user.id):
        await callback.answer("Not available.", show_alert=True)
        return
    groups = await _active_groups(db_session)
    if not groups:
        await callback.message.edit_text(
            "📜 <b>Activity Logs</b>\n\nNo active groups are registered.",
            parse_mode="HTML", reply_markup=_back_kb()
        )
    else:
        await callback.message.edit_text(
            "📜 <b>ACTIVITY LOGS</b>\n\nChoose a group to view its latest recorded actions.",
            parse_mode="HTML",
            reply_markup=_group_picker(groups, "owner:group_logs_detail"),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("owner:group_logs_detail:"))
async def group_logs_detail(callback: CallbackQuery, db_session: AsyncSession):
    if not _is_owner(callback.from_user.id):
        await callback.answer("Not available.", show_alert=True)
        return
    try:
        group_id = int(callback.data.rsplit(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("Invalid group.", show_alert=True)
        return
    group = await db_session.get(Group, group_id)
    if not group or not group.is_active:
        await callback.answer("Group is not active/registered.", show_alert=True)
        return
    logs = (await db_session.scalars(
        select(ActionLog).where(ActionLog.group_id == group_id)
        .order_by(desc(ActionLog.created_at)).limit(10)
    )).all()
    lines = []
    for log in logs:
        when = log.created_at.strftime("%Y-%m-%d %H:%M") if log.created_at else "time unknown"
        actor = f" • actor <code>{log.admin_id}</code>" if log.admin_id else ""
        target = f" • target <code>{log.user_id}</code>" if log.user_id else ""
        lines.append(f"• <b>{escape(log.action or 'Action')}</b> — <code>{escape(when)}</code>{actor}{target}")
    if not lines:
        lines = ["• No activity logs have been recorded for this group yet."]
    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 Refresh", callback_data=f"owner:group_logs_detail:{group_id}")
    builder.button(text="📊 Group Statistics", callback_data=f"owner:group_stats_detail:{group_id}")
    builder.button(text="⚙️ Group Actions", callback_data=f"owner:group:{group_id}")
    builder.button(text="◀️ Logs List", callback_data="owner:group_logs")
    builder.button(text="◀️ Group Activity", callback_data="owner:groups")
    builder.adjust(2, 1, 2)
    await callback.message.edit_text(
        f"📜 <b>Activity Logs — {escape(group.title or 'Untitled')}</b>\n"
        f"🆔 <code>{group.id}</code>\n\n" + "\n".join(lines) +
        "\n\nOnly events saved in Alexa's action log can be shown here.",
        parse_mode="HTML", reply_markup=builder.as_markup()
    )
    await callback.answer()
