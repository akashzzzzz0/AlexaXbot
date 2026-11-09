import asyncio
import hashlib
import logging
import re
import time
from datetime import datetime, timezone
from html import escape

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, ChatMemberUpdated, InlineKeyboardMarkup, Message
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.database.models import Group, GroupPromotionRequest, GroupPromotionOptIn, GlobalSetting

logger = logging.getLogger(__name__)

router = Router(name="alexa_group_promotion")

# Compact persistent snapshot of Telegram's reported administrator rights.
# This uses the existing GlobalSetting table, so no schema migration is needed.
_ADMIN_PERMISSION_FIELDS = (
    ("Manage chat", "can_manage_chat"),
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
    ("Manage direct messages", "can_manage_direct_messages"),
)


def _admin_snapshot_key(group_id: int) -> str:
    return f"alexa_admin_snapshot:{group_id}"


def _encode_admin_snapshot(member, actor_id: int) -> str:
    raw_status = getattr(member, "status", "unknown")
    status = str(getattr(raw_status, "value", raw_status)).lower()
    bits = "".join(
        "?" if getattr(member, attr, None) is None else ("1" if bool(getattr(member, attr)) else "0")
        for _, attr in _ADMIN_PERMISSION_FIELDS
    )
    return f"v1|{status}|{int(time.time())}|{int(actor_id)}|{bits}"[:256]


def _format_admin_snapshot(value: str | None) -> str | None:
    if not value:
        return None
    try:
        version, status, stamp, actor_id, bits = value.split("|", 4)
        if version != "v1" or len(bits) != len(_ADMIN_PERMISSION_FIELDS):
            return None
        observed = datetime.fromtimestamp(int(stamp), timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        lines = [
            "<b>Permissions saved in Alexa database:</b>",
            f"Status at capture: <b>{escape(status.title())}</b>",
            f"Captured: <code>{observed}</code>",
            f"Changed by Telegram user: <code>{actor_id}</code>",
        ]
        for (label, _), bit in zip(_ADMIN_PERMISSION_FIELDS, bits):
            value_text = "YES" if bit == "1" else "NO" if bit == "0" else "NOT EXPOSED"
            icon = "✅" if bit == "1" else "❌" if bit == "0" else "⚪"
            lines.append(f"{icon} {label}: <b>{value_text}</b>")
        return "\n".join(lines)
    except (TypeError, ValueError, OverflowError):
        return None


async def _stored_admin_snapshot(session: AsyncSession, group_id: int) -> str | None:
    row = await session.get(GlobalSetting, _admin_snapshot_key(group_id))
    return _format_admin_snapshot(row.value if row else None)


async def _find_private_group_for_admin(bot, session: AsyncSession, user_id: int):
    """Find the one active Alexa group where the DM sender is also an admin.

    Do not depend on a permission-snapshot row: older groups and Telegram
    membership updates that were handled by the welcome router may have a
    Group row without that auxiliary snapshot. Telegram's live membership
    response is the source of truth for both Alexa and the submitting admin.
    """
    groups = (await session.scalars(
        select(Group)
        .where(Group.is_active.is_(True))
        .order_by(desc(Group.updated_at))
    )).all()
    try:
        me = await bot.get_me()
    except Exception:
        logger.exception("Could not identify Alexa while matching a private group invite")
        return None, None, "Alexa's Telegram identity could not be checked. Please retry."

    matches = []
    for group in groups:
        try:
            bot_member = await bot.get_chat_member(group.id, me.id)
            raw_bot_status = bot_member.status
            bot_status = str(getattr(raw_bot_status, "value", raw_bot_status)).lower()
            if bot_status not in {"administrator", "creator"}:
                continue

            sender_member = await bot.get_chat_member(group.id, user_id)
            raw_sender_status = sender_member.status
            sender_status = str(getattr(raw_sender_status, "value", raw_sender_status)).lower()
            if sender_status not in {"administrator", "creator"}:
                continue

            chat = await bot.get_chat(group.id)
            raw_chat_type = chat.type
            chat_type = str(getattr(raw_chat_type, "value", raw_chat_type)).lower()
            if chat_type not in {"group", "supergroup"}:
                continue
            matches.append((group, chat, bot_member))
            if len(matches) > 1:
                return None, None, (
                    "This admin is an administrator in more than one active Alexa group. "
                    "Telegram does not expose the target group ID from a private invite hash, "
                    "so Alexa cannot safely choose between them."
                )
        except Exception:
            # A stale/inaccessible group is not a valid candidate. Continue
            # checking the remaining active groups rather than failing all.
            logger.debug(
                "Skipping private invite candidate group_id=%s after Telegram lookup failure",
                group.id,
                exc_info=True,
            )
            continue

    if not matches:
        return None, None, (
            "No active group was found where both Alexa and this sender are currently administrators. "
            "Confirm Alexa is still an admin and that the sender is a group admin."
        )

    group, chat, bot_member = matches[0]
    snapshot_key = _admin_snapshot_key(group.id)
    snapshot_row = await session.get(GlobalSetting, snapshot_key)
    snapshot_value = _encode_admin_snapshot(bot_member, user_id)
    if snapshot_row is None:
        session.add(GlobalSetting(key=snapshot_key, value=snapshot_value))
    else:
        snapshot_row.value = snapshot_value

    group.title = chat.title or group.title
    group.username = chat.username
    group.type = str(getattr(chat.type, "value", chat.type))
    group.is_active = True
    await session.flush()
    return group, chat, None

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



_TRIGGER_RE = re.compile(r"(?is)^\s*\.done\b.*|.*t\.me/.*$|^\s*@[A-Za-z0-9_]{5,}\s*$")
_LINK_RE = re.compile(r"(?i)(?:https?://)?t\.me/(?:\+[\w-]+|joinchat/[\w-]+|[A-Za-z0-9_]{5,})|(?<![\w])@[A-Za-z0-9_]{5,}\b")
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
    checks = [
        ("Manage chat", "can_manage_chat"),
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
        ("Manage direct messages", "can_manage_direct_messages"),
    ]
    if status == "creator":
        return "👑 <b>Alexa status: CREATOR</b>\n" + "\n".join(
            f"✅ {label}: <b>YES (creator)</b>" for label, _ in checks
        )
    if status != "administrator":
        return "❌ <b>Administrator permissions:</b> Not available; Alexa is not an administrator."
    lines = ["<b>Alexa's current administrator permissions:</b>"]
    for label, attribute in checks:
        value = getattr(member, attribute, None)
        if value is None:
            lines.append(f"⚪ {label}: <b>NOT EXPOSED BY TELEGRAM</b>")
        else:
            lines.append(f"{'✅' if value else '❌'} {label}: <b>{'YES' if value else 'NO'}</b>")
    return "\n".join(lines)




async def _resolve_private_request_from_recent_add(bot, session: AsyncSession, request: GroupPromotionRequest):
    """Resolve a private invite request from the recent Telegram event that added/promoted Alexa.

    Telegram Bot API cannot dereference an arbitrary private invite hash. We therefore
    use the recorded actor->group event, then independently verify that both Alexa
    and the request sender are still administrators in that exact group.
    """
    setting = await session.get(GlobalSetting, f"promo_added_by:{request.user_id}")
    if setting is None or not setting.value:
        return False, "No recent group-add/admin-change event from this requester is recorded."

    try:
        raw_chat_id, raw_timestamp = setting.value.split(":", 1)
        chat_id = int(raw_chat_id)
        event_timestamp = int(raw_timestamp)
    except (TypeError, ValueError):
        return False, "The saved group-identification record is invalid."

    if time.time() - event_timestamp > 24 * 60 * 60:
        return False, "The group-add/admin-change record is older than 24 hours. Update Alexa's admin rights in the target group and retry."

    group = await session.get(Group, chat_id)
    if group is None:
        return False, "The group record is no longer available. Update Alexa's admin rights in the target group and retry."

    try:
        chat = await bot.get_chat(chat_id)
        chat_type = str(getattr(chat.type, "value", chat.type))
        if chat_type not in {"group", "supergroup"}:
            return False, "The saved chat is not a Telegram group."
        me = await bot.get_me()
        bot_member = await bot.get_chat_member(chat_id, me.id)
        requester_member = await bot.get_chat_member(chat_id, request.user_id)
        bot_status = str(getattr(bot_member.status, "value", bot_member.status)).lower()
        requester_status = str(getattr(requester_member.status, "value", requester_member.status)).lower()
    except Exception:
        logger.exception("Private promotion group re-identification failed for request_id=%s", request.id)
        return False, "Telegram could not confirm current membership. Please retry."

    if bot_status not in {"administrator", "creator"}:
        return False, "Alexa is not an administrator in the recently identified group."
    if requester_status not in {"administrator", "creator"}:
        return False, "The person who sent this link is not an administrator in the recently identified group."

    request.group_id = chat.id
    request.group_title = chat.title or group.title or request.group_title
    group.title = chat.title or group.title
    group.username = chat.username
    group.type = chat_type
    group.is_active = True
    # Store the submitted private invite URL against the verified chat for future exact matches.
    group.invite_link = request.group_link
    await session.commit()
    await session.refresh(request)
    return True, "Private link matched to the most recent group where this requester added or updated Alexa."


async def _verify_and_render_promotion_request(callback: CallbackQuery, session: AsyncSession, request_id: int):
    request = await session.get(GroupPromotionRequest, request_id)
    if not request or request.status not in {"pending", "approved"}:
        await callback.answer("Promotion request not found or no longer active.", show_alert=True)
        return

    if request.group_id <= _UNRESOLVED_PRIVATE_ID_BASE:
        matched, reason = await _resolve_private_request_from_recent_add(callback.bot, session, request)
        if not matched:
            builder = InlineKeyboardBuilder()
            builder.button(text="🔎 Verify Again", callback_data=f"owner:promo_request:verify:{request.id}")
            builder.button(text="❌ Reject Request", callback_data=f"owner:promo_request:reject:{request.id}")
            builder.adjust(1)
            await callback.message.edit_text(
                "🔎 <b>GROUP VERIFICATION — NOT COMPLETED</b>\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"🔗 <b>Submitted link:</b> {escape(request.group_link)}\n"
                "🆔 <b>Group identity:</b> UNKNOWN\n\n"
                "⚠️ Telegram does not let bots open an arbitrary private invite link to discover its chat ID. "
                "Alexa could not safely match this request to a group.\n\n"
                f"ℹ️ <b>Reason:</b> {escape(reason)}\n\n"
                "To create a fresh identification record, update/save Alexa's administrator permissions in the exact target group, "
                "then tap Verify Again. No group command or verification code is required.",
                parse_mode="HTML",
                reply_markup=builder.as_markup(),
                disable_web_page_preview=True,
            )
            await callback.answer("Private group identity is not confirmed.", show_alert=True)
            return

    status, is_admin, can_invite, resolved = await _live_group_proof(callback.bot, request)
    permission_report = await _live_permission_report(callback.bot, request)
    notice_result = "Not sent"
    if resolved:
        if is_admin and can_invite:
            group_notice = (
                "✅ <b>ALEXA GROUP VERIFICATION SUCCESSFUL</b>\n\n"
                f"👥 <b>{escape(request.group_title)}</b>\n"
                "🤖 Alexa membership: <b>Confirmed</b>\n"
                "👑 Administrator status: <b>Confirmed</b>\n"
                "🔗 Invite Users permission: <b>Confirmed</b>\n"
                "📨 The group has been verified for Alexa Free Group Promotion."
            )
        else:
            group_notice = (
                "⚠️ <b>ALEXA GROUP VERIFICATION REPORT</b>\n\n"
                f"👥 <b>{escape(request.group_title)}</b>\n"
                f"🤖 Alexa membership: <b>{escape(status.title())}</b>\n"
                f"👑 Administrator status: <b>{'Confirmed' if is_admin else 'Not confirmed'}</b>\n"
                f"🔗 Invite Users permission: <b>{'Confirmed' if can_invite else 'Missing'}</b>\n"
                "Please update Alexa's administrator permissions before promotion approval."
            )
        try:
            await callback.bot.send_message(request.group_id, group_notice, parse_mode="HTML")
            notice_result = "Sent to the verified group"
        except Exception:
            logger.exception("Could not send group-promotion verification notice for request_id=%s", request.id)
            notice_result = "Telegram could not deliver the group notice"

    proof_text = (
        "🔎 <b>GROUP PROMOTION — LIVE VERIFICATION REPORT</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👥 <b>{escape(request.group_title)}</b>\n"
        f"🔗 <b>Submitted link:</b> {escape(request.group_link)}\n"
        f"🆔 <b>Group ID:</b> <code>{request.group_id if resolved else 'Not available'}</code>\n"
        f"🤖 Alexa membership: <b>{escape(status.title()) if resolved else 'UNKNOWN'}</b>\n"
        f"👑 Alexa administrator: <b>{'YES' if is_admin else 'NO' if resolved else 'UNKNOWN'}</b>\n"
        f"🔗 Invite Users permission: <b>{'YES' if can_invite else 'NO' if resolved else 'UNKNOWN'}</b>\n"
        f"📨 Group verification notice: <b>{escape(notice_result)}</b>\n\n"
        f"{permission_report}\n\n"
        + (
            "✅ <b>Verification successful. The group is eligible for owner approval.</b>"
            if is_admin and can_invite
            else "❌ <b>Verification did not pass.</b> Alexa must be an administrator with Invite Users permission before this group can be published."
            if resolved
            else "⚠️ <b>Group identity remains unknown.</b> Approval is disabled until Alexa can identify the exact group."
        )
    )
    builder = InlineKeyboardBuilder()
    if is_admin and can_invite:
        builder.button(text="✅ Approve & Publish to Channel", callback_data=f"owner:promo_request:approve:{request.id}")
    else:
        builder.button(text="🔎 Verify Again", callback_data=f"owner:promo_request:verify:{request.id}")
    builder.button(text="❌ Reject Request", callback_data=f"owner:promo_request:reject:{request.id}")
    builder.adjust(1)
    await callback.message.edit_text(
        proof_text,
        parse_mode="HTML",
        reply_markup=builder.as_markup(),
        disable_web_page_preview=True,
    )
    await callback.answer("Live verification report refreshed.")


def _is_owner(user_id: int) -> bool:
    return bool(settings.effective_owner_id) and user_id == settings.effective_owner_id


def _normalize_link(link: str) -> str:
    link = link.strip().rstrip(".,;!?)>]")
    if link.startswith("@"):
        return "https://t.me/" + link[1:]
    if not link.startswith(("https://", "http://")):
        link = "https://" + link
    return link.rstrip("/")


def _request_keyboard(request_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(text="🔎 Verify Group", callback_data=f"owner:promo_request:verify:{request_id}")
    builder.button(text="❌ Reject", callback_data=f"owner:promo_request:reject:{request_id}")
    builder.adjust(1)
    return builder.as_markup()


async def _send_owner_request(
    bot,
    request: GroupPromotionRequest,
    bot_status: str = "unknown",
    can_invite_users: bool = False,
    group_resolved: bool = True,
    session: AsyncSession | None = None,
):
    owner_id = settings.effective_owner_id
    if not owner_id:
        return
    permission_report = None
    if group_resolved:
        stored_report = await _stored_admin_snapshot(session, request.group_id) if session is not None else None
        live_report = await _live_permission_report(bot, request)
        permission_report = "\n\n".join(part for part in (stored_report, live_report) if part)
        if "/+" in request.group_link or "/joinchat/" in request.group_link:
            permission_report += (
                "\n\nℹ️ Private invite association uses Alexa's saved group/admin match; "
                "Telegram Bot API cannot decode a private invite hash directly."
            )
    else:
        permission_report = "⚠️ <b>Detailed permissions:</b> Waiting for Alexa to be matched to the exact group."
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
        f"{permission_report}\n\n"
        "<b>Available owner actions:</b> Verify Group or Reject. After live verification succeeds, Alexa will post a verification notice in that group and show Approve & Publish."
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
        # Telegram cannot dereference an arbitrary private invite hash. First
        # use an exact link saved in Alexa's DB; otherwise match only a unique
        # known group where both Alexa and the sender are administrators.
        group = await db_session.scalar(select(Group).where(Group.invite_link == group_link))
        if group is not None:
            known_private_group_id = group.id
            try:
                chat = await message.bot.get_chat(group.id)
            except Exception:
                logger.exception("Known private invite group lookup failed for group_id=%s", group.id)
                chat = None
        if chat is None:
            matched_group, matched_chat, match_error = await _find_private_group_for_admin(
                message.bot, db_session, message.from_user.id
            )
            if matched_group is not None and matched_chat is not None:
                group, chat = matched_group, matched_chat
                known_private_group_id = matched_group.id
                group.invite_link = group_link
                await db_session.flush()
            else:
                group_resolved = False
                if match_error and "more than one" in match_error:
                    await message.answer(
                        "⚠️ <b>Private group link is ambiguous.</b>\n\n" + escape(match_error)
                        + "\n\nSend the group's public link, or make sure Alexa has a unique saved match.",
                        parse_mode="HTML",
                    )
                    return
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
            requester_member = await message.bot.get_chat_member(chat.id, message.from_user.id)
            raw_requester_status = requester_member.status
            requester_status = str(getattr(raw_requester_status, "value", raw_requester_status)).lower()
            if requester_status not in {"administrator", "creator"}:
                await message.answer("⚠️ Only an administrator of this group can submit its link for verification.")
                return
            bot_identity = await message.bot.get_me()
            bot_member = await message.bot.get_chat_member(chat.id, bot_identity.id)
            bot_status = str(getattr(bot_member.status, "value", bot_member.status)).lower()
            can_invite_users = bot_status == "creator" or (
                bot_status == "administrator" and bool(getattr(bot_member, "can_invite_users", False))
            )
            snapshot_key = _admin_snapshot_key(chat.id)
            snapshot_row = await db_session.get(GlobalSetting, snapshot_key)
            snapshot_value = _encode_admin_snapshot(bot_member, message.from_user.id)
            if snapshot_row is None:
                db_session.add(GlobalSetting(key=snapshot_key, value=snapshot_value))
            else:
                snapshot_row.value = snapshot_value
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
        if group_resolved:
            live_status, live_admin, live_invite, live_resolved = await _live_group_proof(message.bot, existing)
            live_report = await _live_permission_report(message.bot, existing)
            stored_report = await _stored_admin_snapshot(db_session, existing.group_id)
            await message.answer(
                "🔎 <b>ALEXA GROUP VERIFICATION</b>\n\n"
                f"👥 <b>{escape(existing.group_title)}</b>\n"
                f"👑 Alexa administrator: <b>{'YES' if live_admin else 'NO' if live_resolved else 'UNKNOWN'}</b>\n"
                f"🔗 Invite Users: <b>{'YES' if live_invite else 'NO' if live_resolved else 'UNKNOWN'}</b>\n\n"
                + "\n\n".join(part for part in (stored_report, live_report) if part)
                + ("\n\nℹ️ Private invite hash was matched through Alexa's unique saved admin-group record; Telegram Bot API cannot decode it directly."
                   if is_private_invite else "")
                + "\n\nYour group is already in the owner's review queue.",
                parse_mode="HTML", disable_web_page_preview=True,
            )
        else:
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

    if group_resolved:
        live_status, live_admin, live_invite, live_resolved = await _live_group_proof(message.bot, request)
        live_report = await _live_permission_report(message.bot, request)
        stored_report = await _stored_admin_snapshot(db_session, request.group_id)
        await message.answer(
            "🔎 <b>ALEXA GROUP VERIFICATION</b>\n\n"
            f"👥 <b>{escape(request.group_title)}</b>\n"
            f"🔗 <b>Submitted link:</b> {escape(request.group_link)}\n"
            f"🆔 Group ID: <code>{request.group_id}</code>\n"
            f"👑 Alexa administrator: <b>{'YES' if live_admin else 'NO' if live_resolved else 'UNKNOWN'}</b>\n"
            f"🔗 Invite Users permission: <b>{'YES' if live_invite else 'NO' if live_resolved else 'UNKNOWN'}</b>\n\n"
            + "\n\n".join(part for part in (stored_report, live_report) if part)
            + ("\n\nℹ️ Private invite hash was matched through Alexa's unique saved admin-group record; Telegram Bot API cannot decode it directly."
               if is_private_invite else "")
            + "\n\n✅ The verification result has also been sent to the bot owner for approval.",
            parse_mode="HTML", disable_web_page_preview=True,
        )
    else:
        await message.answer(
            "⚠️ <b>Link received, but the exact private group is not identified yet.</b>\n\n"
            "Telegram does not reveal a private invite link's chat ID directly. Alexa has saved the request and will only verify it after a safe database match.",
            parse_mode="HTML",
        )
    await _send_owner_request(
        message.bot,
        request,
        bot_status=bot_status,
        can_invite_users=can_invite_users,
        group_resolved=group_resolved,
        session=db_session,
    )

@router.my_chat_member()
async def resolve_private_promotion_when_alexa_is_added(
    event: ChatMemberUpdated,
    db_session: AsyncSession,
    **kwargs,
):
    """Capture Alexa's real group ID on add and resolve a single pending private-link request."""
    if event.chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        raise SkipHandler
    if not event.from_user or event.from_user.is_bot:
        raise SkipHandler

    new_status = str(
        getattr(event.new_chat_member.status, "value", event.new_chat_member.status)
    ).lower()
    if new_status in {"left", "kicked"}:
        raise SkipHandler

    # Telegram supplies the real chat ID whenever Alexa is added or its
    # membership changes, even when Alexa is only a regular member. Capture
    # the group first; permissions are reported accurately below.
    group_type = str(getattr(event.chat.type, "value", event.chat.type))
    detected_group = await db_session.get(Group, event.chat.id)
    if detected_group is None:
        detected_group = await _insert_or_get_group(
            db_session,
            Group(
                id=event.chat.id,
                title=event.chat.title or "Telegram Group",
                username=event.chat.username,
                type=group_type,
                is_active=True,
            ),
        )
    else:
        detected_group.title = event.chat.title or detected_group.title
        detected_group.username = event.chat.username
        detected_group.type = group_type
        detected_group.is_active = True
    await db_session.flush()

    # Remember which group this requester most recently added/promoted Alexa in.
    # The owner can later verify a private invite request without a group command.
    access_key = f"promo_added_by:{event.from_user.id}"
    access_record = await db_session.get(GlobalSetting, access_key)
    access_value = f"{event.chat.id}:{int(time.time())}"
    if access_record is None:
        db_session.add(GlobalSetting(key=access_key, value=access_value))
    else:
        access_record.value = access_value
    snapshot_key = _admin_snapshot_key(event.chat.id)
    snapshot_row = await db_session.get(GlobalSetting, snapshot_key)
    snapshot_value = _encode_admin_snapshot(event.new_chat_member, event.from_user.id)
    if snapshot_row is None:
        db_session.add(GlobalSetting(key=snapshot_key, value=snapshot_value))
    else:
        snapshot_row.value = snapshot_value
    await db_session.commit()

    # A private invite submitted by any group admin can be resolved when
    # Alexa becomes an administrator in that group. Do not attach it while
    # Alexa is only a member; wait for the admin event and verify the sender.
    if new_status not in {"administrator", "creator"}:
        raise SkipHandler

    pending = (await db_session.scalars(
        select(GroupPromotionRequest)
        .where(
            GroupPromotionRequest.group_id <= _UNRESOLVED_PRIVATE_ID_BASE,
            GroupPromotionRequest.status == "pending",
        )
        .order_by(desc(GroupPromotionRequest.requested_at))
        .limit(30)
    )).all()
    matching_requests = []
    for candidate in pending:
        try:
            requester_member = await event.bot.get_chat_member(event.chat.id, candidate.user_id)
            requester_status = str(getattr(requester_member.status, "value", requester_member.status)).lower()
        except Exception:
            continue
        if requester_status in {"administrator", "creator"}:
            matching_requests.append(candidate)
            if len(matching_requests) > 1:
                break
    if len(matching_requests) != 1:
        if len(matching_requests) > 1:
            logger.warning(
                "Private promotion link match is ambiguous among group admins for chat_id=%s",
                event.chat.id,
            )
        raise SkipHandler

    request = matching_requests[0]
    try:
        me = await event.bot.get_me()
        member = await event.bot.get_chat_member(event.chat.id, me.id)
        status = str(getattr(member.status, "value", member.status)).lower()
    except Exception:
        logger.exception("Automatic private promotion verification failed for chat_id=%s", event.chat.id)
        raise SkipHandler
    can_invite = status == "creator" or (
        status == "administrator" and bool(getattr(member, "can_invite_users", False))
    )
    chat_type = str(getattr(event.chat.type, "value", event.chat.type))
    group = await db_session.get(Group, event.chat.id)
    if group is None:
        group = await _insert_or_get_group(
            db_session,
            Group(
                id=event.chat.id,
                title=event.chat.title or request.group_title or "Telegram Group",
                username=event.chat.username,
                type=chat_type,
                is_active=True,
            ),
        )
    else:
        group.title = event.chat.title or group.title
        group.username = event.chat.username
        group.type = chat_type
        group.is_active = True

    # Replace the synthetic unresolved ID with Telegram's actual group ID.
    try:
        async with db_session.begin_nested():
            request.group_id = event.chat.id
            request.group_title = event.chat.title or request.group_title
            await db_session.flush()
    except IntegrityError:
        logger.warning(
            "Automatic private promotion match conflicts with an existing request for chat_id=%s",
            event.chat.id,
        )
        raise SkipHandler

    group.invite_link = request.group_link
    await db_session.commit()
    await db_session.refresh(request)

    # Let the welcome router send the single canonical readiness message.
    await _send_owner_request(
        event.bot,
        request,
        bot_status=status,
        can_invite_users=can_invite,
        group_resolved=True,
        session=db_session,
    )
    raise SkipHandler


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
            "🗂 <b>PROMOTION REQUESTS</b>\n\nNo pending promotion requests.",
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
        status_label = "⏳ Awaiting owner decision" if req.status == "pending" else "🟡 Previously approved — review status"
        lines.append(
            f"#{req.id} · <b>{escape(req.group_title)}</b>\n"
            f"🔗 {escape(req.group_link)}\n"
            f"{status_label}\n"
        )
        builder.button(text=f"🔎 Verify Group #{req.id}", callback_data=f"owner:promo_request:verify:{req.id}")
        builder.button(text=f"❌ Reject #{req.id}", callback_data=f"owner:promo_request:reject:{req.id}")
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
    if action == "approve":
        if request.status not in {"pending", "approved"}:
            await callback.answer("This request has already been completed or rejected.", show_alert=True)
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

        # Mark approved before sending to prevent two simultaneous owner taps
        # from publishing duplicate channel posts. On send failure, restore pending.
        request.status = "approved"
        request.approved_at = request.approved_at or datetime.now(timezone.utc)
        await db_session.commit()

        try:
            channel_id = int(channel_setting.value)
            await callback.bot.send_message(
                channel_id,
                "📣 VERIFIED GROUP — ALEXA FREE PROMOTION 💜\n"
                f"{request.group_link}",
                disable_web_page_preview=False,
            )
        except Exception:
            logger.exception("Failed to publish approved group promotion request_id=%s", request.id)
            request.status = "pending"
            await db_session.commit()
            await callback.answer(
                "Channel post failed. The request is back to Pending, so you can safely review and approve again after fixing channel access.",
                show_alert=True,
            )
            return

        request.status = "promoted"
        request.promoted_at = datetime.now(timezone.utc)
        await db_session.commit()
        try:
            await callback.bot.send_message(
                request.user_id,
                "🎉 <b>Your Group Promotion Has Been Approved!</b>\n\n"
                "💜 Your group has passed verification and its link is now published on our official promotion channel.\n\n"
                "📢 <b>Open the channel to see your completed promotion:</b>\n"
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
        if request.status not in {"pending", "approved"}:
            await callback.answer("This request can no longer be rejected.", show_alert=True)
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




@router.callback_query(F.data.startswith("owner:promo_request:verify:"))
async def promotion_request_verify(callback: CallbackQuery, db_session: AsyncSession, **kwargs):
    if not _is_owner(callback.from_user.id) or not callback.message or callback.message.chat.type != "private":
        await callback.answer("Owner only.", show_alert=True)
        return
    try:
        request_id = int(callback.data.rsplit(":", 1)[1])
    except (TypeError, ValueError):
        await callback.answer("Invalid request.", show_alert=True)
        return
    await _verify_and_render_promotion_request(callback, db_session, request_id)

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
    if not request or request.status not in {"pending", "approved"}:
        await callback.answer("This request is no longer awaiting review.", show_alert=True)
        return

    status, is_admin, can_invite, resolved = await _live_group_proof(callback.bot, request)
    group_unknown = request.group_id <= _UNRESOLVED_PRIVATE_ID_BASE
    permission_report = await _live_permission_report(callback.bot, request)
    proof_text = (
        "🔎 <b>GROUP PROMOTION — VERIFICATION PROOF</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👥 <b>{escape(request.group_title)}</b>\n"
        f"🔗 <b>Group Link:</b> {escape(request.group_link)}\n"
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


