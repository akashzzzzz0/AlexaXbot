"""Owner-configurable, automatic Force Join enforcement."""
import asyncio
import time

from aiogram import Router, F
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from bot.config import settings
from bot.database.models import Group
from bot.database.force_join import ForceJoinSetting
from bot.handlers.owner import is_owner

logger = structlog.get_logger()
router = Router()
_notice_cooldowns: dict[tuple[int, int], float] = {}


class ForceJoinStates(StatesGroup):
    waiting_destination = State()


def is_required_member(status, is_member=None) -> bool:
    """Normalize Telegram member status values, including restricted members."""
    value = str(getattr(status, "value", status)).lower()
    if value == "restricted":
        return bool(is_member)
    return value in {"member", "administrator", "creator"}


def _groups_keyboard(groups):
    builder = InlineKeyboardBuilder()
    for group in groups:
        builder.button(text=f"👥 {group.title[:45]}", callback_data=f"owner:forcejoin:group:{group.id}")
    builder.button(text="◀️ Owner Panel", callback_data="owner:menu")
    builder.adjust(1)
    return builder.as_markup()


def _detail_keyboard(group_id: int, setting: ForceJoinSetting | None):
    builder = InlineKeyboardBuilder()
    if setting and setting.required_chat_id and setting.required_link:
        builder.button(
            text="🟢 Disable Force Join" if setting.enabled else "🔴 Enable Force Join",
            callback_data=f"owner:forcejoin:toggle:{group_id}",
        )
    builder.button(text="✏️ Set / Change Required Channel or GC", callback_data=f"owner:forcejoin:configure:{group_id}")
    builder.button(text="📋 Choose Another GC", callback_data="owner:forcejoin")
    builder.button(text="◀️ Owner Panel", callback_data="owner:menu")
    builder.adjust(1)
    return builder.as_markup()


async def _show_group(callback: CallbackQuery, session: AsyncSession, group_id: int):
    group = await session.get(Group, group_id)
    if not group or not group.is_active:
        await callback.answer("This group is not active/registered.", show_alert=True)
        return
    setting = await session.scalar(select(ForceJoinSetting).where(ForceJoinSetting.group_id == group_id))
    if setting and setting.required_chat_id:
        destination = (
            f"Required chat: <code>{setting.required_chat_id}</code>\n"
            f"Join link: {setting.required_link}\n"
            f"Status: <b>{'ON' if setting.enabled else 'OFF'}</b>"
        )
    else:
        destination = "No required channel/group configured yet.\nStatus: <b>OFF</b>"
    await callback.message.edit_text(
        f"🔗 <b>Force Join · {group.title}</b>\n\n"
        f"Protected GC ID: <code>{group.id}</code>\n"
        f"{destination}\n\n"
        "Members who have not joined the required destination have their group messages deleted. "
        "After joining, Alexa checks membership automatically on their next message; no Verify button is needed.",
        parse_mode="HTML",
        reply_markup=_detail_keyboard(group_id, setting),
        disable_web_page_preview=True,
    )


@router.callback_query(F.data == "owner:forcejoin")
async def force_join_groups(callback: CallbackQuery, db_session: AsyncSession, **kwargs):
    if not is_owner(callback.from_user.id):
        await callback.answer("Not available.", show_alert=True)
        return
    groups = (await db_session.scalars(
        select(Group).where(Group.is_active.is_(True)).order_by(Group.title.asc())
    )).all()
    if not groups:
        await callback.message.edit_text(
            "No active groups are registered yet. Add Alexa to a group first.",
            reply_markup=_groups_keyboard([]),
        )
    else:
        await callback.message.edit_text(
            "🔗 <b>OWNER FORCE JOIN</b>\n\nChoose the GC where members must join a channel/group before messaging.",
            parse_mode="HTML",
            reply_markup=_groups_keyboard(groups),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("owner:forcejoin:group:"))
async def force_join_group_detail(callback: CallbackQuery, db_session: AsyncSession, **kwargs):
    if not is_owner(callback.from_user.id):
        await callback.answer("Not available.", show_alert=True)
        return
    try:
        group_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Invalid group.", show_alert=True)
        return
    await _show_group(callback, db_session, group_id)
    await callback.answer()


@router.callback_query(F.data.startswith("owner:forcejoin:configure:"))
async def force_join_configure(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession, **kwargs):
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
    await state.set_state(ForceJoinStates.waiting_destination)
    await state.update_data(force_join_group_id=group_id)
    await callback.message.answer(
        f"Send the required destination for <b>{group.title}</b> in this format:\n\n"
        "<code>@channelusername | https://t.me/channelusername</code>\n"
        "or for a private channel/group:\n"
        "<code>-1001234567890 | https://t.me/+invitecode</code>\n\n"
        "The first part must be the @username or numeric chat ID Alexa can check. "
        "The second part is the Join button link. Alexa must be an admin in the required channel/group.",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(ForceJoinStates.waiting_destination, F.chat.type == "private")
async def force_join_save_destination(message: Message, state: FSMContext, db_session: AsyncSession, **kwargs):
    if not message.from_user or not is_owner(message.from_user.id):
        await state.clear()
        return
    parts = (message.text or "").split("|", 1)
    if len(parts) != 2:
        await message.answer("Format required: <code>@username or -100chatid | https://t.me/joinlink</code>", parse_mode="HTML")
        return
    target, link = (part.strip() for part in parts)
    if not (target.startswith("@") or (target.startswith("-") and target[1:].isdigit())):
        await message.answer("First part must be an @channel/group username or numeric chat ID.")
        return
    if not (link.startswith("https://t.me/") or link.startswith("http://t.me/")):
        await message.answer("Join link must start with https://t.me/ (or http://t.me/).")
        return
    data = await state.get_data()
    group_id = data.get("force_join_group_id")
    group = await db_session.get(Group, int(group_id)) if group_id else None
    if not group or not group.is_active:
        await state.clear()
        await message.answer("That protected group is no longer active. Open Owner Panel → Force Join again.")
        return
    setting = await db_session.scalar(select(ForceJoinSetting).where(ForceJoinSetting.group_id == group.id))
    if setting is None:
        setting = ForceJoinSetting(group_id=group.id, required_chat_id=target, required_link=link, enabled=False)
        db_session.add(setting)
    else:
        setting.required_chat_id = target
        setting.required_link = link
        setting.enabled = False
    await db_session.commit()
    await state.clear()
    await message.answer(
        f"✅ Required destination saved for <b>{group.title}</b>.\n"
        "Force Join is currently OFF. Return to Owner Panel → Force Join → this group to enable it.",
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("owner:forcejoin:toggle:"))
async def force_join_toggle(callback: CallbackQuery, db_session: AsyncSession, **kwargs):
    if not is_owner(callback.from_user.id):
        await callback.answer("Not available.", show_alert=True)
        return
    try:
        group_id = int(callback.data.rsplit(":", 1)[1])
    except ValueError:
        await callback.answer("Invalid group.", show_alert=True)
        return
    group = await db_session.get(Group, group_id)
    setting = await db_session.scalar(select(ForceJoinSetting).where(ForceJoinSetting.group_id == group_id))
    if not group or not setting:
        await callback.answer("Configure a required destination first.", show_alert=True)
        return
    if not setting.enabled:
        try:
            target_bot = await callback.bot.get_chat_member(setting.required_chat_id, callback.bot.id)
            target_status = str(getattr(target_bot.status, "value", target_bot.status))
            if target_status not in {"administrator", "creator"}:
                await callback.answer("Alexa must be an admin in the required channel/group before enabling.", show_alert=True)
                return
            protected_bot = await callback.bot.get_chat_member(group_id, callback.bot.id)
            protected_status = str(getattr(protected_bot.status, "value", protected_bot.status))
            if protected_status not in {"administrator", "creator"}:
                await callback.answer("Alexa must be an admin in the protected GC.", show_alert=True)
                return
            if protected_status == "administrator" and not getattr(protected_bot, "can_delete_messages", False):
                await callback.answer("Give Alexa Delete Messages permission in the protected GC.", show_alert=True)
                return
        except Exception as exc:
            logger.warning("force_join_enable_permission_check_failed", group_id=group_id, error=str(exc))
            await callback.answer("Alexa could not verify permissions. Check the required chat ID and bot admin access.", show_alert=True)
            return
    setting.enabled = not setting.enabled
    await db_session.commit()
    await _show_group(callback, db_session, group_id)
    await callback.answer("Force Join enabled." if setting.enabled else "Force Join disabled.")


@router.message(F.chat.type.in_({"group", "supergroup"}))
async def enforce_force_join(message: Message, db_session: AsyncSession, **kwargs):
    """Delete messages from unjoined members; let all other message handlers continue."""
    if not message.from_user or message.from_user.is_bot:
        raise SkipHandler
    setting = await db_session.scalar(
        select(ForceJoinSetting).where(
            ForceJoinSetting.group_id == message.chat.id,
            ForceJoinSetting.enabled.is_(True),
        )
    )
    if not setting:
        raise SkipHandler
    if settings.effective_owner_id and message.from_user.id == settings.effective_owner_id:
        raise SkipHandler
    try:
        current = await message.bot.get_chat_member(message.chat.id, message.from_user.id)
        current_status = str(getattr(current.status, "value", current.status))
        if current_status in {"administrator", "creator"}:
            raise SkipHandler
    except SkipHandler:
        raise
    except Exception as exc:
        logger.warning("force_join_could_not_check_group_role", group_id=message.chat.id, user_id=message.from_user.id, error=str(exc))
        raise SkipHandler
    try:
        required = await message.bot.get_chat_member(setting.required_chat_id, message.from_user.id)
    except Exception as exc:
        logger.warning("force_join_could_not_check_required_membership", group_id=message.chat.id, user_id=message.from_user.id, error=str(exc))
        raise SkipHandler
    if is_required_member(required.status, getattr(required, "is_member", None)):
        raise SkipHandler
    try:
        await message.delete()
    except Exception as exc:
        logger.warning("force_join_message_delete_failed", group_id=message.chat.id, message_id=message.message_id, error=str(exc))
    key = (message.chat.id, message.from_user.id)
    now = time.monotonic()
    if now - _notice_cooldowns.get(key, 0) >= 15:
        _notice_cooldowns[key] = now
        builder = InlineKeyboardBuilder()
        builder.button(text="📢 Join Required Channel / GC", url=setting.required_link)
        notice = await message.answer(
            f"🔒 <a href=\"tg://user?id={message.from_user.id}\">{message.from_user.full_name}</a>, "
            "pehle required channel/group join karein. Join karne ke baad Alexa aglay message par automatically check karegi — Verify button ki zaroorat nahi.",
            parse_mode="HTML",
            reply_markup=builder.as_markup(),
            disable_web_page_preview=True,
        )
        async def _remove_notice():
            await asyncio.sleep(30)
            try:
                await notice.delete()
            except Exception:
                pass
        asyncio.create_task(_remove_notice())
