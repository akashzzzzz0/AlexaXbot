import asyncio
import re
from datetime import datetime, timezone
from html import escape

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.database.models import Group, GroupPromotionRequest

router = Router(name="alexa_group_promotion")

_TRIGGER_RE = re.compile(r"(?is)^\s*\.done\b.*|.*t\.me/.*$")
_LINK_RE = re.compile(r"(?i)(?:https?://)?t\.me/(?:\+[\w-]+|joinchat/[\w-]+|[A-Za-z0-9_]{5,})")


def _is_owner(user_id: int) -> bool:
    return bool(settings.effective_owner_id) and user_id == settings.effective_owner_id


def _normalize_link(link: str) -> str:
    link = link.strip().rstrip(".,;!?)>]")
    if not link.startswith(("https://", "http://")):
        link = "https://" + link
    return link.rstrip("/")


def _request_keyboard(request_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Approve & Promote", callback_data=f"owner:promo_request:approve:{request_id}")
    builder.button(text="❌ Reject", callback_data=f"owner:promo_request:reject:{request_id}")
    builder.adjust(1, 1)
    return builder.as_markup()


async def _send_owner_request(bot, request: GroupPromotionRequest):
    owner_id = settings.effective_owner_id
    if not owner_id:
        return
    text = (
        "📣 <b>NEW FREE GROUP PROMOTION REQUEST</b>\n\n"
        f"👥 <b>{escape(request.group_title)}</b>\n"
        f"🔗 {escape(request.group_link)}\n"
        f"👤 Requester ID: <code>{request.user_id}</code>\n"
        f"🆔 Group ID: <code>{request.group_id}</code>\n"
        f"📋 Request: <code>#{request.id}</code>\n\n"
        "Alexa verified that she is an administrator in this group. Review the request below."
    )
    try:
        await bot.send_message(owner_id, text, parse_mode="HTML", reply_markup=_request_keyboard(request.id), disable_web_page_preview=True)
    except Exception:
        # The request remains available in Owner Panel → Promotion Requests.
        pass


@router.message(F.chat.type == ChatType.PRIVATE, F.text.regexp(_TRIGGER_RE))
async def receive_group_promotion_link(message: Message, db_session: AsyncSession, **kwargs):
    text = (message.text or "").strip()
    match = _LINK_RE.search(text)
    if not match:
        await message.answer(
            "Please send your group link, either by itself or after <code>.done</code>.\n\n"
            "Example: <code>.done https://t.me/YourGroup</code>",
            parse_mode="HTML",
        )
        return

    group_link = _normalize_link(match.group(0))
    is_private_invite = "/+" in group_link or "/joinchat/" in group_link
    group = None
    chat = None

    if is_private_invite:
        known_groups = (await db_session.scalars(
            select(Group).where(Group.is_active.is_(True), Group.invite_link.is_not(None))
        )).all()
        group = next((g for g in known_groups if _normalize_link(g.invite_link or "") == group_link), None)
        if group is None:
            await message.answer(
                "I couldn't match this private invite link to a group I can verify. "
                "Please send the group's public <code>t.me/GroupName</code> link, or ask the owner to register the private invite link first.",
                parse_mode="HTML",
            )
            return
        try:
            chat = await message.bot.get_chat(group.id)
        except Exception:
            await message.answer("I couldn't access that group to verify it. Please check the link and try again.")
            return
    else:
        slug = group_link.split("t.me/", 1)[1].split("?", 1)[0].strip("/")
        if not slug or "/" in slug:
            await message.answer("Please send a valid public group link such as <code>https://t.me/YourGroup</code>.", parse_mode="HTML")
            return
        try:
            chat = await message.bot.get_chat("@" + slug)
        except Exception:
            await message.answer(
                "I couldn't find that group. Please check the link, add Alexa to the group, promote Alexa to Admin, and send the link again."
            )
            return

    chat_type = getattr(chat.type, "value", chat.type)
    if chat_type not in {"group", "supergroup"}:
        await message.answer("That link doesn't point to a Telegram group. Please send a group link.")
        return

    try:
        bot_member = await message.bot.get_chat_member(chat.id, message.bot.id)
        bot_status = str(getattr(bot_member.status, "value", bot_member.status))
    except Exception:
        bot_status = "unknown"
    if bot_status not in {"administrator", "creator"}:
        await message.answer(
            "Oops! 💜 It looks like you haven't added Alexa to your group as an Admin yet.\n\n"
            "Please add me to your group and promote me to Admin first. Then send your group link again, "
            "and I'll verify it for you. ✨",
        )
        return

    group = await db_session.get(Group, chat.id)
    if not group:
        group = Group(
            id=chat.id,
            title=chat.title or "Telegram Group",
            username=chat.username,
            type=chat_type,
            is_active=True,
            invite_link=group_link if is_private_invite else None,
        )
        db_session.add(group)
    else:
        group.title = chat.title or group.title
        group.username = chat.username
        group.type = chat_type
        group.is_active = True
        if is_private_invite:
            group.invite_link = group_link
    await db_session.flush()

    existing = await db_session.scalar(
        select(GroupPromotionRequest)
        .where(
            GroupPromotionRequest.group_id == chat.id,
            GroupPromotionRequest.status.in_(["pending", "approved"]),
        )
        .order_by(desc(GroupPromotionRequest.id))
        .limit(1)
    )
    if existing:
        await db_session.commit()
        if existing.status == "pending":
            await message.answer("💜 Your group is already on the Free Group Promotion list and is waiting for the owner's verification.")
        else:
            await message.answer("💜 Your group has already been approved and is waiting for promotion to finish.")
        return

    request = GroupPromotionRequest(
        user_id=message.from_user.id,
        group_id=chat.id,
        group_title=chat.title or "Telegram Group",
        group_link=group_link,
        status="pending",
    )
    db_session.add(request)
    await db_session.commit()
    await db_session.refresh(request)

    await message.answer(
        "Yay! 🎉💜 Your group has been added to the Free Group Promotion list!\n\n"
        "⏳ Your request is now waiting for the owner's verification. Once approved, your group will be promoted "
        "within <b>24 hours of approval</b>. 🚀\n\n"
        "Thank you for choosing Alexa! 💖",
        parse_mode="HTML",
    )
    await _send_owner_request(message.bot, request)


async def _run_promotion(request: GroupPromotionRequest, bot, session: AsyncSession):
    targets = (await session.scalars(
        select(Group).where(Group.is_active.is_(True), Group.id != request.group_id)
    )).all()
    if not targets:
        return 0, 0

    builder = InlineKeyboardBuilder()
    builder.button(text="🔗 Join Group", url=request.group_link)
    markup = builder.as_markup()
    title = escape(request.group_title)
    text = (
        "📣 <b>FREE GROUP PROMOTION</b>\n\n"
        f"👥 <b>{title}</b>\n\n"
        "Discover and join this community using the button below. 💜"
    )
    sent = 0
    failed = 0
    for group in targets:
        try:
            await bot.send_message(group.id, text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
            sent += 1
        except TelegramRetryAfter as exc:
            await asyncio.sleep(max(1, int(exc.retry_after)))
            try:
                await bot.send_message(group.id, text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
                sent += 1
            except Exception:
                failed += 1
        except (TelegramForbiddenError, Exception):
            failed += 1
        await asyncio.sleep(0.05)
    return sent, failed


@router.callback_query(F.data == "owner:promo_requests")
async def promotion_requests_panel(callback: CallbackQuery, db_session: AsyncSession, **kwargs):
    if not _is_owner(callback.from_user.id) or not callback.message or callback.message.chat.type != "private":
        await callback.answer("Owner only.", show_alert=True)
        return
    requests = (await db_session.scalars(
        select(GroupPromotionRequest)
        .where(GroupPromotionRequest.status.in_(["pending", "approved"]))
        .order_by(desc(GroupPromotionRequest.requested_at))
        .limit(30)
    )).all()
    if not requests:
        await callback.message.edit_text(
            "🗂 <b>PROMOTION REQUESTS</b>\n\nNo pending or retryable promotion requests.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                {"text": "◀️ Owner Panel", "callback_data": "owner:menu"}
            ]]),
        )
        await callback.answer()
        return
    builder = InlineKeyboardBuilder()
    lines = ["🗂 <b>PROMOTION REQUESTS</b>", "━━━━━━━━━━━━━━━━━━", ""]
    for req in requests:
        status_label = "⏳ Pending owner approval" if req.status == "pending" else "🟡 Approved — retry if needed"
        lines.append(f"#{req.id} · <b>{escape(req.group_title)}</b>\n{escape(req.group_link)}\n{status_label}\n")
        if req.status == "pending":
            builder.button(text=f"✅ Approve #{req.id}", callback_data=f"owner:promo_request:approve:{req.id}")
            builder.button(text=f"❌ Reject #{req.id}", callback_data=f"owner:promo_request:reject:{req.id}")
        else:
            builder.button(text=f"🚀 Retry promotion #{req.id}", callback_data=f"owner:promo_request:retry:{req.id}")
    builder.button(text="🔄 Refresh", callback_data="owner:promo_requests")
    builder.button(text="◀️ Owner Panel", callback_data="owner:menu")
    builder.adjust(1)
    await callback.message.edit_text("\n".join(lines)[:3900], parse_mode="HTML", reply_markup=builder.as_markup(), disable_web_page_preview=True)
    await callback.answer()


async def _process_owner_promotion_action(callback: CallbackQuery, db_session: AsyncSession, request_id: int, action: str):
    request = await db_session.get(GroupPromotionRequest, request_id)
    if not request:
        await callback.answer("Promotion request not found.", show_alert=True)
        return
    if action in {"approve", "retry"}:
        if action == "approve":
            if request.status != "pending":
                await callback.answer("This request has already been reviewed.", show_alert=True)
                return
            request.status = "approved"
            request.approved_at = datetime.now(timezone.utc)
            await db_session.commit()
        elif request.status != "approved":
            await callback.answer("Only approved requests can be retried.", show_alert=True)
            return

        sent, failed = await _run_promotion(request, callback.bot, db_session)
        if sent:
            request.status = "promoted"
            request.promoted_at = datetime.now(timezone.utc)
            await db_session.commit()
            try:
                await callback.bot.send_message(
                    request.user_id,
                    "🎉💜 The owner has approved your group promotion! Your group promotion has now started. "
                    f"Alexa shared it with <b>{sent}</b> active groups. 🚀",
                    parse_mode="HTML",
                )
            except Exception:
                pass
            await callback.answer(f"Promotion sent to {sent} groups. Failed: {failed}.")
            nav = InlineKeyboardBuilder()
            nav.button(text="🗂 Promotion Requests", callback_data="owner:promo_requests")
            nav.button(text="👑 Owner Panel", callback_data="owner:menu")
            nav.adjust(1)
            await callback.message.edit_text(
                f"✅ <b>Promotion #{request.id} processed</b>\\n\\n"
                f"Sent to active groups: <b>{sent}</b>\\nFailed: <b>{failed}</b>",
                parse_mode="HTML",
                reply_markup=nav.as_markup(),
            )
            return

        await db_session.commit()
        await callback.answer(
            "No group received the promotion. Keep this request approved and retry when active groups are available.",
            show_alert=True,
        )
        return

    if action == "reject":
        if request.status != "pending":
            await callback.answer("Only pending requests can be rejected.", show_alert=True)
            return
        request.status = "rejected"
        await db_session.commit()
        try:
            await callback.bot.send_message(
                request.user_id,
                "Thanks for submitting your group for Free Group Promotion. 💜 "
                "The owner couldn't approve this request at the moment. You can contact the owner for more information.",
            )
        except Exception:
            pass
        await callback.answer("Promotion request rejected.")
        nav = InlineKeyboardBuilder()
        nav.button(text="🗂 Promotion Requests", callback_data="owner:promo_requests")
        nav.button(text="👑 Owner Panel", callback_data="owner:menu")
        nav.adjust(1)
        await callback.message.edit_text(
            f"❌ <b>Promotion request #{request.id} rejected</b>",
            parse_mode="HTML",
            reply_markup=nav.as_markup(),
        )


@router.callback_query(F.data.startswith("owner:promo_request:approve:"))
async def promotion_request_approve(callback: CallbackQuery, db_session: AsyncSession, **kwargs):
    if not _is_owner(callback.from_user.id) or not callback.message or callback.message.chat.type != "private":
        await callback.answer("Owner only.", show_alert=True)
        return
    try:
        request_id = int(callback.data.rsplit(":", 1)[1])
    except (TypeError, ValueError):
        await callback.answer("Invalid request.", show_alert=True)
        return
    await _process_owner_promotion_action(callback, db_session, request_id, "approve")


@router.callback_query(F.data.startswith("owner:promo_request:reject:"))
async def promotion_request_reject(callback: CallbackQuery, db_session: AsyncSession, **kwargs):
    if not _is_owner(callback.from_user.id) or not callback.message or callback.message.chat.type != "private":
        await callback.answer("Owner only.", show_alert=True)
        return
    try:
        request_id = int(callback.data.rsplit(":", 1)[1])
    except (TypeError, ValueError):
        await callback.answer("Invalid request.", show_alert=True)
        return
    await _process_owner_promotion_action(callback, db_session, request_id, "reject")


@router.callback_query(F.data.startswith("owner:promo_request:retry:"))
async def promotion_request_retry(callback: CallbackQuery, db_session: AsyncSession, **kwargs):
    if not _is_owner(callback.from_user.id) or not callback.message or callback.message.chat.type != "private":
        await callback.answer("Owner only.", show_alert=True)
        return
    try:
        request_id = int(callback.data.rsplit(":", 1)[1])
    except (TypeError, ValueError):
        await callback.answer("Invalid request.", show_alert=True)
        return
    await _process_owner_promotion_action(callback, db_session, request_id, "retry")
