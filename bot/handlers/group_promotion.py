import asyncio
import hashlib
import logging
import re
from datetime import datetime, timezone
from html import escape

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.database.models import Group, GroupPromotionRequest, GroupPromotionOptIn, GlobalSetting

logger = logging.getLogger(__name__)

router = Router(name="alexa_group_promotion")

async def _insert_or_get_group(session: AsyncSession, candidate: Group) -> Group:
    try:
        async with session.begin_nested():
            session.add(candidate)
            await session.flush()
        return candidate
    except IntegrityError:
        group = await session.get(Group, candidate.id)
        if group is None:
            raise
        return group



_TRIGGER_RE = re.compile(r"(?is)^\s*\.done\b.*|.*t\.me/.*$")
_LINK_RE = re.compile(r"(?i)(?:https?://)?t\.me/(?:\+[\w-]+|joinchat/[\w-]+|[A-Za-z0-9_]{5,})")
_PROMOTION_STATUS_RE = re.compile(
    r"(?i).*\b(?:promotion|promote|promoting)\b.*"
    r"|\b(?:abhi tak|still waiting)\b"
)
_PROMOTION_NOTICE = (
    "✅ <b>Your group link has been received successfully!</b>\n\n"
    "🔍 Alexa will check its group membership and required admin permissions.\n"
    "Your verification report will be reviewed by the owner. If approved, your group will be published on our official channel.\n\n"
    "📢 <b>Join our channel to view your group promotion:</b>\n"
    "https://t.me/+tdfGw6ekfNA4OTNk\n\n"
    "💜 Thank you for choosing Alexa Free Group Promotion!"
)

# Telegram chat IDs do not use this reserved range; it marks an invite link
# whose target chat cannot be resolved through the Bot API.
_UNRESOLVED_PRIVATE_ID_BASE = -(1 << 60)


def _unresolved_private_group_id(link: str) -> int:
    digest = hashlib.sha256(link.encode("utf-8")).hexdigest()[:14]
    return _UNRESOLVED_PRIVATE_ID_BASE - int(digest, 16)


async def _live_group_proof(bot, request: GroupPromotionRequest) -> tuple[str, bool, bool, bool]:
    if request.group_id <= _UNRESOLVED_PRIVATE_ID_BASE:
        return "unknown", False, False, False
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(request.group_id, me.id)
        status = str(getattr(member.status, "value", member.status)).lower()
        is_admin = status in {"administrator", "creator"}
        can_invite = is_admin and (
            status == "creator" or bool(getattr(member, "can_invite_users", False))
        )
        return status, is_admin, can_invite, True
    except Exception:
        logger.exception("Could not refresh group-promotion proof for request_id=%s", request.id)
        return "unknown", False, False, True



async def _live_permission_report(bot, request: GroupPromotionRequest) -> str:
    """Return all administrator permissions exposed by the current Telegram API."""
    if request.group_id <= _UNRESOLVED_PRIVATE_ID_BASE:
        return "⚠️ Permissions: UNKNOWN (private invite target is unresolved)."
    try:
        me = await bot.get_me()
        member = await bot.get_chat_member(request.group_id, me.id)
    except Exception:
        logger.exception("Detailed permission check failed for request_id=%s", request.id)
        return "⚠️ Permissions: UNKNOWN (live Telegram check failed)."
    status = str(getattr(member.status, "value", member.status)).lower()
    if status == "creator":
        return "👑 <b>Creator:</b> YES — all administrator permissions"
    if status != "administrator":
        return "❌ <b>Administrator permissions:</b> Not available; Alexa is not an administrator."
    checks = [
        ("Change group info", "can_change_info"),
        ("Delete messages", "can_delete_messages"),
        ("Restrict / ban members", "can_restrict_members"),
        ("Invite users / create invite links", "can_invite_users"),
        ("Pin messages", "can_pin_messages"),
        ("Promote administrators", "can_promote_members"),
        ("Manage video chats", "can_manage_video_chats"),
        ("Manage topics", "can_manage_topics"),
        ("Post messages", "can_post_messages"),
        ("Edit messages", "can_edit_messages"),
        ("Post stories", "can_post_stories"),
        ("Edit stories", "can_edit_stories"),
        ("Delete stories", "can_delete_stories"),
    ]
    lines = ["<b>Alexa's current administrator permissions:</b>"]
    for label, attribute in checks:
        value = getattr(member, attribute, None)
        if value is not None:
            lines.append(f"{'✅' if value else '❌'} {label}: <b>{'YES' if value else 'NO'}</b>")
    return "\n".join(lines)


def _is_owner(user_id: int) -> bool:
    return bool(settings.effective_owner_id) and user_id == settings.effective_owner_id


def _normalize_link(link: str) -> str:
    link = link.strip().rstrip(".,;!?)>]")
    if not link.startswith(("https://", "http://")):
        link = "https://" + link
    return link.rstrip("/")


def _request_keyboard(request_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(text="🔎 View Verification Proof", callback_data=f"owner:promo_request:proof:{request_id}")
    builder.button(text="❌ Reject", callback_data=f"owner:promo_request:reject:{request_id}")
    builder.adjust(1)
    return builder.as_markup()


async def _send_owner_request(
    bot,
    request: GroupPromotionRequest,
    bot_status: str = "unknown",
    can_invite_users: bool = False,
    group_resolved: bool = True,
):
    owner_id = settings.effective_owner_id
    if not owner_id:
        return
    text = (
        "📣 <b>NEW FREE GROUP PROMOTION REQUEST</b>\n\n"
        f"👥 <b>{escape(request.group_title)}</b>\n"
        f"🔗 {escape(request.group_link)}\n"
        f"👤 Requester ID: <code>{request.user_id}</code>\n"
        f"🆔 Group ID: <code>{'Not available' if request.group_id <= _UNRESOLVED_PRIVATE_ID_BASE else request.group_id}</code>\n"
        f"📋 Request: <code>#{request.id}</code>\n\n"
        "🔎 <b>VERIFICATION SNAPSHOT</b>\n"
        f"{'✅' if group_resolved else '⚠️'} Group identity: <b>{'Resolved' if group_resolved else 'Not resolvable by Telegram Bot API'}</b>\n"
        f"{'✅' if bot_status in {'administrator', 'creator'} else '❌' if group_resolved and bot_status != 'unknown' else '❔'} Alexa admin: <b>{'YES' if bot_status in {'administrator', 'creator'} else 'NO' if group_resolved and bot_status != 'unknown' else 'UNKNOWN'}</b>\n"
        f"{'✅' if can_invite_users else '❌' if group_resolved and bot_status != 'unknown' else '❔'} Invite-link right: <b>{'YES' if can_invite_users else 'NO' if group_resolved and bot_status != 'unknown' else 'UNKNOWN'}</b>\n\n"
        "Tap <b>View Verification Proof</b> to refresh the live rights check. Approve & Publish appears only when both required rights are confirmed."
    )
    try:
        await bot.send_message(owner_id, text, parse_mode="HTML", reply_markup=_request_keyboard(request.id), disable_web_page_preview=True)
    except Exception:
        # The request remains available in Owner Panel → Promotion Requests.
        pass

@router.message(StateFilter(None), F.chat.type == ChatType.PRIVATE, F.forward_origin)
async def capture_promotion_channel(message: Message, db_session: AsyncSession, **kwargs):
    """Save the channel ID from an owner-forwarded channel post."""
    if not message.from_user or not _is_owner(message.from_user.id):
        return
    origin = message.forward_origin
    origin_type = str(getattr(getattr(origin, "type", None), "value", getattr(origin, "type", ""))).lower()
    channel = getattr(origin, "chat", None)
    if origin_type != "channel" or channel is None:
        await message.answer("Please forward a message directly from your Alexa Promotion Channel.")
        return
    setting = await db_session.get(GlobalSetting, "group_promotion_channel_id")
    if setting is None:
        setting = GlobalSetting(key="group_promotion_channel_id", value=str(channel.id))
        db_session.add(setting)
    else:
        setting.value = str(channel.id)
    await db_session.commit()
    await message.answer(
        "✅ <b>Promotion channel connected!</b>\n\n"
        f"Channel: <b>{escape(getattr(channel, 'title', None) or 'Alexa Promotion Channel')}</b>\n"
        f"Channel ID: <code>{channel.id}</code>\n\n"
        "Approved group links will be posted only in this channel.",
        parse_mode="HTML",
    )


@router.message(StateFilter(None), F.chat.type == ChatType.PRIVATE, F.text.regexp(_TRIGGER_RE))
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
    known_private_group_id = None
    group_resolved = True
    bot_status = "unknown"
    can_invite_users = False

    if is_private_invite:
        # Bot API cannot turn an arbitrary invite hash into a chat ID. If Alexa
        # already knows the exact link, use that chat ID; otherwise queue a
        # reviewable request marked UNKNOWN instead of silently dropping it.
        group = await db_session.scalar(select(Group).where(Group.invite_link == group_link))
        if group is not None:
            known_private_group_id = group.id
            try:
                chat = await message.bot.get_chat(group.id)
            except Exception:
                logger.exception("Known private invite group lookup failed for group_id=%s", group.id)
                chat = None
        if chat is None and known_private_group_id is None:
            group_resolved = False
    else:
        slug = group_link.split("t.me/", 1)[1].split("?", 1)[0].strip("/")
        if not slug or "/" in slug:
            await message.answer(
                "⚠️ <b>This does not look like a supported direct group link.</b>\n\n"
                "Please send a direct Telegram group link.",
                parse_mode="HTML",
            )
            return
        try:
            chat = await message.bot.get_chat("@" + slug)
        except Exception:
            logger.exception("Group promotion verification: get_chat failed for public group link %s", group_link)
            await message.answer(
                "⚠️ <b>Alexa could not resolve this group link.</b>\n\n"
                "Please check the link and send it again.",
                parse_mode="HTML",
            )
            return

    if chat is not None:
        chat_type = getattr(chat.type, "value", chat.type)
        if chat_type not in {"group", "supergroup"}:
            await message.answer(
                "⚠️ <b>This link does not point to a Telegram group.</b>",
                parse_mode="HTML",
            )
            return
        try:
            bot_identity = await message.bot.get_me()
            bot_member = await message.bot.get_chat_member(chat.id, bot_identity.id)
            bot_status = str(getattr(bot_member.status, "value", bot_member.status)).lower()
            can_invite_users = bot_status == "creator" or (
                bot_status == "administrator" and bool(getattr(bot_member, "can_invite_users", False))
            )
        except Exception:
            logger.exception("Group promotion verification: get_chat_member failed for chat_id=%s", chat.id)
            bot_status = "unknown"
            can_invite_users = False

        group_id = chat.id
        group_title = chat.title or "Telegram Group"
        # Save/update known groups only when Alexa actually has admin rights.
        if bot_status in {"administrator", "creator"}:
            group = await db_session.get(Group, chat.id)
            if not group:
                group = await _insert_or_get_group(
                    db_session,
                    Group(
                        id=chat.id,
                        title=group_title,
                        username=chat.username,
                        type=chat_type,
                        is_active=True,
                        invite_link=group_link if is_private_invite else None,
                    ),
                )
            group.title = group.title or group_title
            group.username = chat.username
            group.type = chat_type
            group.is_active = True
            if is_private_invite:
                group.invite_link = group_link
            await db_session.flush()
    elif known_private_group_id is not None:
        # The exact invite link is mapped to a known group, even if get_chat
        # temporarily failed. The owner proof callback can retry live membership.
        group_id = known_private_group_id
        group_title = group.title or "Known private Telegram group"
    else:
        # A stable synthetic key lets the owner see/reject an unresolved private
        # invite request. It is never eligible for approval or channel publishing.
        group_id = _unresolved_private_group_id(group_link)
        group_title = "Unresolved private invite link"

    existing = await db_session.scalar(
        select(GroupPromotionRequest)
        .where(
            GroupPromotionRequest.group_id == group_id,
            GroupPromotionRequest.status.in_(["pending", "approved", "promoted"]),
        )
        .order_by(desc(GroupPromotionRequest.id))
        .limit(1)
    )
    if existing:
        await db_session.commit()
        await message.answer(_PROMOTION_NOTICE, parse_mode="HTML")
        return

    request = GroupPromotionRequest(
        user_id=message.from_user.id,
        group_id=group_id,
        group_title=group_title,
        group_link=group_link,
        status="pending",
    )
    try:
        async with db_session.begin_nested():
            db_session.add(request)
            await db_session.flush()
    except IntegrityError:
        await message.answer(_PROMOTION_NOTICE, parse_mode="HTML")
        return
    await db_session.commit()
    await db_session.refresh(request)

    await message.answer(_PROMOTION_NOTICE, parse_mode="HTML")
    await _send_owner_request(
        message.bot,
        request,
        bot_status=bot_status,
        can_invite_users=can_invite_users,
        group_resolved=group_resolved,
    )

@router.message(
    F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
    F.text.regexp(re.compile(r"(?i)^/promotions(?:@\w+)?\s+(on|off)\s*$")),
)
async def set_group_promotion_opt_in(message: Message, db_session: AsyncSession, **kwargs):
    """Let a group administrator explicitly opt the group in or out."""
    if not message.from_user or message.from_user.is_bot:
        return
    try:
        member = await message.bot.get_chat_member(message.chat.id, message.from_user.id)
        status = str(getattr(member.status, "value", member.status)).lower()
    except Exception:
        await message.answer("I couldn't verify your admin permissions. Please try again.")
        return
    if status not in {"administrator", "creator"}:
        await message.answer("Only group admins can change promotion consent.")
        return

    enabled = (message.text or "").strip().lower().split()[-1] == "on"
    group = await db_session.get(Group, message.chat.id)
    if group is None:
        group = await _insert_or_get_group(
            db_session,
            Group(
                id=message.chat.id,
                title=message.chat.title or "Telegram Group",
                username=message.chat.username,
                type=getattr(message.chat.type, "value", message.chat.type),
                is_active=True,
            ),
        )
    else:
        group.title = message.chat.title or group.title
        group.username = message.chat.username
        group.is_active = True
    row = await db_session.get(GroupPromotionOptIn, message.chat.id)
    if row is None:
        if not enabled:
            await message.answer("This group is already opted out of cross-group promotions.")
            return
        row = GroupPromotionOptIn(
            group_id=message.chat.id,
            enabled=True,
            enabled_by=message.from_user.id,
        )
        db_session.add(row)
    else:
        row.enabled = enabled
        row.enabled_by = message.from_user.id
    await db_session.commit()
    if enabled:
        await message.answer("✅ This group has opted in to Alexa cross-group promotions. Admins can use /promotions off anytime.")
    else:
        await message.answer("🔕 This group has opted out. Alexa will no longer send cross-group promotion posts here.")


@router.message(
    F.chat.type == ChatType.PRIVATE,
    F.text.regexp(_PROMOTION_STATUS_RE),
)
async def answer_promotion_status(message: Message, db_session: AsyncSession, **kwargs):
    """Answer promotion follow-ups from the user's actual request status."""
    if not message.from_user or (message.text or "").lstrip().startswith("/"):
        return

    request = await db_session.scalar(
        select(GroupPromotionRequest)
        .where(GroupPromotionRequest.user_id == message.from_user.id)
        .order_by(desc(GroupPromotionRequest.requested_at))
        .limit(1)
    )
    if request is None:
        await message.answer(
            "🔗 Please send your group's direct link (for example, <code>https://t.me/YourGroup</code>) "
            "so Alexa can verify it and check the promotion request.",
            parse_mode="HTML",
        )
        return

    if request.status == "pending":
        await message.answer(
            "👑 <b>Your Group Is on Alexa's Promotion List!</b> 💜\n\n"
            "⏳ Your request is awaiting review. Please be patient; we aim to promote verified groups within "
            "<b>24 hours</b>.\n\n"
            "✨ Thank you for growing with Alexa!",
            parse_mode="HTML",
        )
    elif request.status == "approved":
        await message.answer(
            "💜 Your group request is approved and awaiting promotion. "
            "Please be patient while Alexa completes the promotion within <b>24 hours</b>. 🚀",
            parse_mode="HTML",
        )
    elif request.status == "promoted":
        await message.answer(
            "🎉💜 Alexa has already started promoting your group! "
            "Thank you for being part of the Alexa community. 👑",
            parse_mode="HTML",
        )
    else:
        await message.answer(
            "💜 I couldn't find an active promotion request for your group. "
            "Please send your group's direct link again so Alexa can verify it.",
            parse_mode="HTML",
        )


async def _run_promotion(request: GroupPromotionRequest, bot, session: AsyncSession):
    targets = (await session.scalars(
        select(Group)
        .join(GroupPromotionOptIn, GroupPromotionOptIn.group_id == Group.id)
        .where(
            Group.is_active.is_(True),
            GroupPromotionOptIn.enabled.is_(True),
            Group.id != request.group_id,
        )
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
        lines.append(
            f"#{req.id} · <b>{escape(req.group_title)}</b>\n"
            f"{escape(req.group_link)}\n"
            "🔎 Tap View Proof to refresh Alexa's current admin and invite-link rights\n"
            f"{status_label}\n"
        )
        if req.status == "pending":
            builder.button(text=f"🔎 View Proof #{req.id}", callback_data=f"owner:promo_request:proof:{req.id}")
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
        elif request.status != "approved":
            await callback.answer("Only approved requests can be retried.", show_alert=True)
            return

        proof_status, proof_is_admin, proof_can_invite, proof_resolved = await _live_group_proof(callback.bot, request)
        if not proof_resolved or not proof_is_admin or not proof_can_invite:
            await callback.answer(
                "Cannot publish: Alexa's group identity, admin status, and invite-link permission must all be confirmed.",
                show_alert=True,
            )
            return

        channel_setting = await db_session.get(GlobalSetting, "group_promotion_channel_id")
        if channel_setting is None or not channel_setting.value:
            await callback.answer(
                "Promotion channel is not connected. Forward a channel post to Alexa first.",
                show_alert=True,
            )
            return

        if action == "approve":
            request.status = "approved"
            request.approved_at = datetime.now(timezone.utc)
            await db_session.commit()

        try:
            channel_id = int(channel_setting.value)
            builder = InlineKeyboardBuilder()
            builder.button(text="🔗 Join Group", url=request.group_link)
            await callback.bot.send_message(
                channel_id,
                "📣 <b>VERIFIED GROUP — ALEXA FREE PROMOTION</b> 💜\n\n"
                f"👥 <b>{escape(request.group_title)}</b>\n\n"
                f"🔗 <a href=\"{escape(request.group_link, quote=True)}\">Open Group Link</a>\n\n"
                "Join this community using the link above or the button below. 🚀",
                parse_mode="HTML",
                reply_markup=builder.as_markup(),
                disable_web_page_preview=True,
            )
        except Exception:
            logger.exception("Failed to publish approved group promotion request_id=%s", request.id)
            await callback.answer(
                "Could not post to the connected channel. Check Alexa's channel posting permission and retry.",
                show_alert=True,
            )
            return

        request.status = "promoted"
        request.promoted_at = datetime.now(timezone.utc)
        await db_session.commit()
        try:
            await callback.bot.send_message(
                request.user_id,
                "🎉 <b>Your Group Link Verification Is Complete!</b>\n\n"
                "💜 Your group has been successfully verified and approved by our team.\n\n"
                "🚀 Your group link has been published on the Alexa Promotion Channel.\n\n"
                "📢 Visit our channel:\n"
                "https://t.me/+tdfGw6ekfNA4OTNk\n\n"
                "👑 Thank you for choosing Alexa Free Group Promotion!",
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception:
            logger.exception("Channel post succeeded but requester DM failed request_id=%s", request.id)
        await callback.answer("Approved group link posted to the promotion channel.")
        nav = InlineKeyboardBuilder()
        nav.button(text="🗂 Promotion Requests", callback_data="owner:promo_requests")
        nav.button(text="👑 Owner Panel", callback_data="owner:menu")
        nav.adjust(1)
        await callback.message.edit_text(
            f"✅ <b>Promotion #{request.id} published to the configured channel.</b>",
            parse_mode="HTML",
            reply_markup=nav.as_markup(),
        )

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


@router.callback_query(F.data.startswith("owner:promo_request:proof:"))
async def promotion_request_proof(callback: CallbackQuery, db_session: AsyncSession, **kwargs):
    if not _is_owner(callback.from_user.id) or not callback.message or callback.message.chat.type != "private":
        await callback.answer("Owner only.", show_alert=True)
        return
    try:
        request_id = int(callback.data.rsplit(":", 1)[1])
    except (TypeError, ValueError):
        await callback.answer("Invalid request.", show_alert=True)
        return
    request = await db_session.get(GroupPromotionRequest, request_id)
    if not request or request.status != "pending":
        await callback.answer("This request is no longer pending.", show_alert=True)
        return

    status, is_admin, can_invite, resolved = await _live_group_proof(callback.bot, request)
    group_unknown = request.group_id <= _UNRESOLVED_PRIVATE_ID_BASE
    permission_report = await _live_permission_report(callback.bot, request)
    proof_text = (
        "🔎 <b>GROUP PROMOTION — VERIFICATION PROOF</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👥 <b>{escape(request.group_title)}</b>\n"
        f"🔗 <code>{escape(request.group_link)}</code>\n"
        f"🆔 Group ID: <code>{'Not available' if group_unknown else request.group_id}</code>\n"
        f"🤖 Alexa membership: <b>{escape(status.title()) if resolved else 'UNKNOWN'}</b>\n"
        f"👑 Alexa is administrator: <b>{'YES' if is_admin else 'UNKNOWN' if status == 'unknown' or not resolved else 'NO'}</b>\n"
        f"🔗 Invite Users via Link right: <b>{'YES' if can_invite else 'UNKNOWN' if status == 'unknown' or not resolved else 'NO'}</b>\n\n"
        + permission_report + "\n\n"
        + (
            "✅ <b>Both required permissions are confirmed.</b> You can approve and publish."
            if is_admin and can_invite
            else "❌ <b>Required permissions are not confirmed.</b> Reject this request, or ask the user to add Alexa with the required rights."
            if resolved
            else "⚠️ <b>Telegram Bot API cannot resolve this arbitrary private invite link to a group.</b> Alexa cannot verify membership or rights from this link, so publishing is disabled."
        )
    )
    builder = InlineKeyboardBuilder()
    if is_admin and can_invite:
        builder.button(text="✅ Approve & Publish to Channel", callback_data=f"owner:promo_request:approve:{request.id}")
    builder.button(text="❌ Reject Request", callback_data=f"owner:promo_request:reject:{request.id}")
    builder.button(text="◀️ Promotion Requests", callback_data="owner:promo_requests")
    builder.adjust(1)
    await callback.message.edit_text(
        proof_text,
        parse_mode="HTML",
        reply_markup=builder.as_markup(),
        disable_web_page_preview=True,
    )
    await callback.answer("Live verification proof refreshed.")


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
