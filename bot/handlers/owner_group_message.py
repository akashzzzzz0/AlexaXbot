from aiogram import Router, F
from aiogram.types import CallbackQuery, Message
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from html import escape

from bot.config import settings
from bot.database.models import Group

router = Router()


def _is_owner(user_id: int) -> bool:
    return bool(settings.effective_owner_id) and user_id == settings.effective_owner_id


class SelectedGroupMessageStates(StatesGroup):
    composing = State()
    confirming = State()


def _selection_keyboard(groups, selected_ids: set[int]):
    builder = InlineKeyboardBuilder()
    for group in groups:
        selected = group.id in selected_ids
        title = (group.title or str(group.id)).replace("\n", " ")[:42]
        builder.button(
            text=f"{'✅' if selected else '⬜'} {title}",
            callback_data=f"owner:group_message:toggle:{group.id}",
        )
    builder.button(text="➡️ Compose Message", callback_data="owner:group_message:compose")
    builder.button(text="❌ Cancel", callback_data="owner:group_message:cancel")
    builder.button(text="◀️ Owner Panel", callback_data="owner:menu")
    builder.adjust(1)
    return builder.as_markup()


async def _show_selection(callback: CallbackQuery, session: AsyncSession, state: FSMContext):
    groups = (await session.scalars(
        select(Group).where(Group.is_active.is_(True)).order_by(Group.title.asc())
    )).all()
    data = await state.get_data()
    selected_ids = set(data.get("selected_group_ids", []))
    if not groups:
        await callback.message.edit_text(
            "📨 <b>Message Selected Groups</b>\n\nNo active groups are registered.",
            parse_mode="HTML",
        )
        return
    chosen = sum(1 for group in groups if group.id in selected_ids)
    await callback.message.edit_text(
        "📨 <b>MESSAGE SELECTED GROUPS</b>\n\n"
        "Select one or more groups below. Only selected groups will receive the message.\n"
        f"\nSelected: <b>{chosen}</b> / {len(groups)}\n\n"
        "After selecting groups, tap <b>Compose Message</b>.",
        parse_mode="HTML",
        reply_markup=_selection_keyboard(groups, selected_ids),
    )


@router.callback_query(F.data == "owner:group_message")
async def selected_group_message_open(callback: CallbackQuery, db_session: AsyncSession, state: FSMContext, **kwargs):
    if not _is_owner(callback.from_user.id) or not callback.message or callback.message.chat.type != "private":
        await callback.answer("Not available.", show_alert=True)
        return
    await state.clear()
    await state.update_data(selected_group_ids=[])
    await _show_selection(callback, db_session, state)
    await callback.answer()


@router.callback_query(F.data.startswith("owner:group_message:toggle:"))
async def selected_group_message_toggle(callback: CallbackQuery, db_session: AsyncSession, state: FSMContext, **kwargs):
    if not _is_owner(callback.from_user.id) or not callback.message or callback.message.chat.type != "private":
        await callback.answer("Not available.", show_alert=True)
        return
    try:
        group_id = int(callback.data.rsplit(":", 1)[1])
    except (TypeError, ValueError):
        await callback.answer("Invalid group.", show_alert=True)
        return
    group = await db_session.get(Group, group_id)
    if not group or not group.is_active:
        await callback.answer("This group is no longer active.", show_alert=True)
        return
    data = await state.get_data()
    selected_ids = set(data.get("selected_group_ids", []))
    if group_id in selected_ids:
        selected_ids.remove(group_id)
    else:
        selected_ids.add(group_id)
    await state.update_data(selected_group_ids=list(selected_ids))
    await _show_selection(callback, db_session, state)
    await callback.answer("Group selection updated.")


@router.callback_query(F.data == "owner:group_message:compose")
async def selected_group_message_compose(callback: CallbackQuery, state: FSMContext, **kwargs):
    if not _is_owner(callback.from_user.id) or not callback.message or callback.message.chat.type != "private":
        await callback.answer("Not available.", show_alert=True)
        return
    data = await state.get_data()
    selected_ids = data.get("selected_group_ids", [])
    if not selected_ids:
        await callback.answer("Select at least one group first.", show_alert=True)
        return
    await state.set_state(SelectedGroupMessageStates.composing)
    await callback.message.edit_text(
        f"✍️ <b>Compose group message</b>\n\n"
        f"Selected groups: <b>{len(selected_ids)}</b>\n"
        "Send the text message you want Alexa to deliver. Nothing will be sent until you confirm.",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(StateFilter(SelectedGroupMessageStates.composing))
async def selected_group_message_preview(message: Message, state: FSMContext, **kwargs):
    if not _is_owner(message.from_user.id) or message.chat.type != "private":
        return
    content = message.text
    if not content or not content.strip():
        await message.answer("Please send a text message. Media-only messages are not supported in this option.")
        return
    data = await state.get_data()
    selected_ids = data.get("selected_group_ids", [])
    if not selected_ids:
        await state.clear()
        await message.answer("No groups selected. Open the Owner Panel and try again.")
        return
    await state.update_data(pending_group_message=content)
    await state.set_state(SelectedGroupMessageStates.confirming)
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Confirm & Send", callback_data="owner:group_message:send")
    builder.button(text="✏️ Rewrite Message", callback_data="owner:group_message:rewrite")
    builder.button(text="❌ Cancel", callback_data="owner:group_message:cancel")
    builder.adjust(1)
    await message.answer(
        "👁 <b>MESSAGE PREVIEW</b>\n\n"
        f"Target groups: <b>{len(selected_ids)}</b>\n\n"
        f"<pre>{escape(content[:3500])}</pre>\n\n"
        "Check the preview. Alexa will send only after you tap Confirm & Send.",
        parse_mode="HTML",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(F.data == "owner:group_message:rewrite")
async def selected_group_message_rewrite(callback: CallbackQuery, state: FSMContext, **kwargs):
    if not _is_owner(callback.from_user.id) or not callback.message or callback.message.chat.type != "private":
        await callback.answer("Not available.", show_alert=True)
        return
    await state.set_state(SelectedGroupMessageStates.composing)
    await callback.message.edit_text("✍️ Send the replacement text. It will not be sent until confirmed.")
    await callback.answer()


@router.callback_query(F.data == "owner:group_message:send")
async def selected_group_message_send(callback: CallbackQuery, db_session: AsyncSession, state: FSMContext, **kwargs):
    if not _is_owner(callback.from_user.id) or not callback.message or callback.message.chat.type != "private":
        await callback.answer("Not available.", show_alert=True)
        return
    data = await state.get_data()
    selected_ids = set(data.get("selected_group_ids", []))
    content = data.get("pending_group_message")
    if not selected_ids or not isinstance(content, str) or not content.strip():
        await state.clear()
        await callback.answer("Message draft expired. Start again.", show_alert=True)
        return
    groups = (await db_session.scalars(
        select(Group).where(Group.id.in_(selected_ids), Group.is_active.is_(True))
    )).all()
    await callback.answer("Sending message…")
    sent, failed = [], []
    for group in groups:
        try:
            await callback.bot.send_message(chat_id=group.id, text=content, parse_mode=None, disable_web_page_preview=False)
            sent.append(group)
        except Exception:
            failed.append(group)
    missing_ids = selected_ids - {group.id for group in groups}
    failed.extend([type("MissingGroup", (), {"id": group_id, "title": str(group_id)})() for group_id in missing_ids])
    await state.clear()
    details = []
    details.append(f"✅ Sent: <b>{len(sent)}</b>")
    details.append(f"❌ Failed: <b>{len(failed)}</b>")
    if failed:
        details.append("\n<b>Failed groups</b>")
        details.extend(f"• {escape((group.title or str(group.id))[:60])} (<code>{group.id}</code>)" for group in failed[:20])
    if len(failed) > 20:
        details.append(f"…and {len(failed) - 20} more.")
    builder = InlineKeyboardBuilder()
    builder.button(text="◀️ Owner Panel", callback_data="owner:menu")
    await callback.message.edit_text(
        "📨 <b>SELECTED-GROUP MESSAGE REPORT</b>\n\n" + "\n".join(details),
        parse_mode="HTML",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(F.data == "owner:group_message:cancel")
async def selected_group_message_cancel(callback: CallbackQuery, state: FSMContext, **kwargs):
    if not _is_owner(callback.from_user.id) or not callback.message or callback.message.chat.type != "private":
        await callback.answer("Not available.", show_alert=True)
        return
    await state.clear()
    builder = InlineKeyboardBuilder()
    builder.button(text="◀️ Owner Panel", callback_data="owner:menu")
    await callback.message.edit_text("❌ Selected-group message cancelled. Nothing was sent.", reply_markup=builder.as_markup())
    await callback.answer("Cancelled.")
