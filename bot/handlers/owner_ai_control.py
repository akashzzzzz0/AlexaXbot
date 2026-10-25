"""Owner-only natural-language control for ALEXA.

Only explicitly addressed messages from the configured owner are considered.
The model may classify requests, but execution is restricted to a small allowlist.
"""
import html
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import get_gemini_api_key, settings
from bot.database.models import (GlobalSetting, Group, GroupMember, GroupSettings, SecuritySettings, User, Warning,
    Economy, Wallet, Referral, AffiliateRequest, ActionLog, GroupStats, InboxMessage, ScamReview)
from bot.services.moderation_service import ban_user, unban_user, kick_user, mute_user, unmute_user, warn_user

router = Router(name="owner_ai_control")
logger = logging.getLogger(__name__)
_OWNER_PREFIX = re.compile(r"^\s*(?:alexa\b|@alexasuper_bot\b)[\s,!:.-]*(.*)$", re.I)
_BOT_USERNAME = None
_PK = ZoneInfo("Asia/Kolkata")


class OwnerAIState(StatesGroup):
    waiting_prompt = State()


def _is_owner(user_id: int | None) -> bool:
    return bool(user_id and settings.effective_owner_id and user_id == settings.effective_owner_id)


async def _global_ai_enabled(session: AsyncSession) -> bool:
    row = await session.get(GlobalSetting, "owner_ai_enabled")
    return row is None or row.value == "1"


async def _set_global_ai_enabled(session: AsyncSession, enabled: bool) -> None:
    row = await session.get(GlobalSetting, "owner_ai_enabled")
    if row is None:
        session.add(GlobalSetting(key="owner_ai_enabled", value="1" if enabled else "0"))
    else:
        row.value = "1" if enabled else "0"
    await session.commit()


async def _find_target_group(message: Message, session: AsyncSession, prompt: str):
    """Resolve a registered group named in the request, or use the current group."""
    groups = (await session.scalars(
        select(Group).where(Group.is_active.is_(True)).order_by(Group.title.asc())
    )).all()
    lowered = (prompt or "").casefold()
    # An explicit registered group title always wins over the current chat.
    for group in groups:
        if group.title and group.title.casefold() in lowered:
            return group
    numeric = re.search(r"(?<!\d)(-100\d{8,}|-\d{5,20})(?!\d)", prompt or "")
    if numeric:
        try:
            group_id = int(numeric.group(1))
            group = await session.get(Group, group_id)
            if group and group.is_active:
                return group
            # The bot may be an admin in a group not yet tracked in the database.
            chat = await message.bot.get_chat(group_id)
            chat_type = str(getattr(chat.type, "value", chat.type))
            if chat_type in {"group", "supergroup"}:
                group = Group(id=group_id, title=chat.title or "Telegram group", type=chat_type, is_active=True)
                session.add(group)
                await session.commit()
                return group
        except Exception:
            await session.rollback()
    if message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        current = await session.get(Group, message.chat.id)
        if current and current.is_active:
            return current
        current = Group(
            id=message.chat.id,
            title=message.chat.title or "Current group",
            type=str(getattr(message.chat.type, "value", message.chat.type)),
            is_active=True,
        )
        session.add(current)
        await session.commit()
        return current
    return None


async def _lookup_user(session: AsyncSession, prompt: str) -> str:
    username = re.search(r"(?<!\w)@([A-Za-z0-9_]{5,32})\b", prompt)
    numeric = re.search(r"(?<!\d)(\d{5,20})(?!\d)", prompt)
    if numeric:
        user = await session.get(User, int(numeric.group(1)))
        rows = [user] if user else []
    elif username:
        user = await session.scalar(select(User).where(func.lower(User.username) == username.group(1).casefold()))
        rows = [user] if user else []
    else:
        query = re.sub(r"(?i)\b(owner\s+panel|add\s+members|database|records?|recorded|please|alexa|find|search|lookup|look up|who is|tell me|show me|user|members?|id|telegram|saved|from|mein|me|se|say|nikaal|nikal|lo|lao|ki|ka|ke|hai|he|batao|btao|mujhe|mere|meri|iska|iski|naam|name|details|profile|about|list)\b", " ", prompt)
        query = re.sub(r"\s+", " ", query).strip(" @#:-")
        if len(query) < 2:
            return "Kis user ka naam, @username ya Telegram ID check karni hai?"
        rows = list((await session.scalars(select(User).where(or_(
            func.lower(User.first_name).contains(query.casefold()),
            func.lower(User.last_name).contains(query.casefold()),
            func.lower(User.username).contains(query.casefold()),
        )).order_by(User.first_name.asc()).limit(10))).all())
    if not rows:
        return "Is naam se exact match clear nahi mila. @username, numeric Telegram ID, ya us user ke group message ka reply bhejo taake main sahi profile identify kar sakoon."
    lines = ["🔎 <b>Alexa saved-user search</b>"]
    for user in rows:
        if not user:
            continue
        name = html.escape(" ".join(x for x in [user.first_name, user.last_name] if x) or "Unknown")
        username_text = f" (@{html.escape(user.username)})" if user.username else ""
        lines.append(f"\n• <b>{name}</b>{username_text}\n  🆔 <code>{user.id}</code>\n  DM started: <b>{'Yes' if user.dm_started else 'No'}</b>")
    return "\n".join(lines)


async def _explicitly_addressed(message: Message) -> bool:
    global _BOT_USERNAME
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        return False
    if _OWNER_PREFIX.match(text):
        return True
    if message.reply_to_message and message.reply_to_message.from_user and message.reply_to_message.from_user.id == message.bot.id:
        return True
    if _BOT_USERNAME is None:
        try:
            me = await message.bot.get_me()
            _BOT_USERNAME = (me.username or "").casefold()
        except Exception:
            _BOT_USERNAME = ""
    return bool(_BOT_USERNAME and re.search(rf"(?<!\w)@{re.escape(_BOT_USERNAME)}\b", text, re.I))


def _strip_prefix(text: str) -> str:
    match = _OWNER_PREFIX.match(text or "")
    if match:
        return match.group(1).strip()
    if _BOT_USERNAME:
        return re.sub(rf"(?<!\w)@{re.escape(_BOT_USERNAME)}\b", "", text or "", flags=re.I).strip()
    return (text or "").strip()


async def _classify_request(text: str) -> dict:
    """Use Gemini only to classify into a fixed, validated action schema."""
    key = get_gemini_api_key()
    if not key:
        raise RuntimeError("Gemini is not configured")
    model = (settings.GEMINI_MODEL or "gemini-2.5-flash").strip()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    instructions = (
        "Classify the user's Roman Urdu/English request into JSON only. "
        "Allowed action values: report_today, list_groups, list_users, lookup_user, user_details, referral_report, group_activity, bot_health, global_controls_status, inbox_summary, group_settings_status, group_status, lock_content, unlock_content, ban, mute, kick, promote, demote, demote_self, promote_self, make_bot_admin, unban, unmute, warn, reset_warnings, help, unsupported. "
        "Schema: {\"action\": string, \"type\": string, \"duration_seconds\": integer, \"reason\": string}. "
        "For lock/unlock, type must be one of text, links, photos, videos, stickers, all. "
        "For global daily user counts choose report_today. For listing active registered groups choose list_groups. For listing recent recorded users choose list_users. For a user's saved name, username or Telegram ID or full saved profile choose lookup_user or user_details. For referral/Stars requests choose referral_report. For group activity choose group_activity. For bot process/database status choose bot_health. For global anti-spam/flood/lockdown overview choose global_controls_status. For unread user inbox counts choose inbox_summary. For group security/lock configuration choose group_settings_status. "
        "For current chat bot permissions/status choose group_status. If owner says remove me/myself from admin, choose demote_self. If owner says make me/myself admin, choose promote_self. If owner asks to make Alexa/the bot an admin, choose make_bot_admin (never pretend the bot can promote itself). For ban/mute/kick/promote/demote/unban/unmute/warn/reset warnings choose the matching action only when clearly requested. Mute duration is seconds, default 3600. Keep reason short. Target is resolved separately from an explicit mention, reply, or user ID. If not clearly supported choose unsupported. "
        "Never output code, commands, explanations, or extra JSON fields."
    )
    payload = {
        "systemInstruction": {"parts": [{"text": instructions}]},
        "contents": [{"role": "user", "parts": [{"text": text[:1500]}]}],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 100, "responseMimeType": "application/json"},
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(12.0, connect=5.0)) as client:
        response = await client.post(url, params={"key": key}, json=payload)
        response.raise_for_status()
        data = response.json()
    parts = (data.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
    raw = "".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()
    result = json.loads(raw)
    action = result.get("action")
    kind = str(result.get("type", "")).lower()
    allowed = {"report_today", "list_groups", "list_users", "lookup_user", "user_details", "referral_report", "group_activity", "bot_health", "global_controls_status", "inbox_summary", "group_settings_status", "group_status", "lock_content", "unlock_content", "ban", "mute", "kick", "promote", "demote", "demote_self", "promote_self", "make_bot_admin", "unban", "unmute", "warn", "reset_warnings", "help", "unsupported"}
    if action not in allowed:
        return {"action": "unsupported", "type": ""}
    if action in {"lock_content", "unlock_content"} and kind not in {"text", "links", "photos", "videos", "stickers", "all"}:
        return {"action": "unsupported", "type": ""}
    return {"action": action, "type": kind, "duration_seconds": max(60, min(int(result.get("duration_seconds") or 3600), 2419200)), "reason": str(result.get("reason") or "Owner AI command")[:180]}


async def _answer_owner_question(prompt: str) -> str:
    """General owner Q&A; this never executes actions or invents private account access."""
    key = get_gemini_api_key()
    if not key:
        return "General AI answer ke liye Gemini connection configured nahi hai. Koi action execute nahi hua."
    model = (settings.GEMINI_MODEL or "gemini-2.5-flash").strip()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    system = (
        "You are Alexa, the owner's helpful Roman Urdu/Hinglish assistant. Answer general knowledge, explanations, "
        "planning and everyday questions directly and clearly in the user's language. Be honest about uncertainty. "
        "Never claim you accessed Telegram's private/global directory, private chats, phone numbers, or data not supplied "
        "or stored by this bot. Never claim a moderation/configuration action was performed. If the request asks for a bot action "
        "that the control layer has not implemented, say it is not connected yet. Do not expose API keys or internal setup."
    )
    payload = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": prompt[:3000]}]}],
        "generationConfig": {"temperature": 0.55, "maxOutputTokens": 500},
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=8.0)) as client:
            response = await client.post(url, params={"key": key}, json=payload)
            response.raise_for_status()
            data = response.json()
        candidates = data.get("candidates") or []
        parts = candidates[0].get("content", {}).get("parts", []) if candidates else []
        answer = " ".join(p.get("text", "").strip() for p in parts if isinstance(p, dict) and p.get("text")).strip()
        if not answer:
            return "Is sawal ka clear jawab generate nahi ho saka—dobara thora detail se poochho."
        return html.escape(answer[:3500])
    except Exception as exc:
        logger.warning("Owner general Q&A failed: %s", type(exc).__name__)
        return "Abhi general AI answer nahi aa saka. Gemini connection/model check karo."


async def _today_report(session: AsyncSession) -> str:
    now_local = datetime.now(_PK)
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = (start_local + timedelta(days=1)).astimezone(timezone.utc)
    new_users = await session.scalar(
        select(func.count(User.id)).where(User.created_at >= start_utc, User.created_at < end_utc)
    )
    total_users = await session.scalar(select(func.count(User.id)))
    active_groups = await session.scalar(
        select(func.count(Group.id)).where(Group.is_active.is_(True))
    )
    return (
        "📊 <b>ALEXA — Aaj ki global report</b>\n"
        f"👤 Aaj registered naye users: <b>{new_users or 0}</b>\n"
        f"👥 Total registered users: <b>{total_users or 0}</b>\n"
        f"🌐 Active registered groups: <b>{active_groups or 0}</b>\n"
        f"🕒 Date (India): <b>{now_local.strftime('%d %b %Y')}</b>\n\n"
        "<i>Counts database ke recorded users par based hain; Telegram ke tamam users ka count nahi.</i>"
    )


async def _groups_report(session: AsyncSession) -> str:
    rows = (await session.scalars(
        select(Group).where(Group.is_active.is_(True)).order_by(Group.title.asc()).limit(30)
    )).all()
    if not rows:
        return "Abhi database mein koi active registered group nahi mila."
    lines = [f"• {html.escape(g.title or 'Untitled')} — <code>{g.id}</code>" for g in rows]
    more = "\n<i>Showing up to 30 groups.</i>" if len(rows) == 30 else ""
    return "🌐 <b>Active registered groups</b>\n\n" + "\n".join(lines) + more



async def _users_report(session: AsyncSession, limit: int = 20) -> str:
    rows = (await session.scalars(
        select(User).order_by(User.created_at.desc(), User.id.desc()).limit(limit)
    )).all()
    if not rows:
        return "Alexa ke database mein abhi koi saved user record nahi hai."
    lines = ["👥 <b>Recent saved users</b>"]
    for user in rows:
        name = html.escape(" ".join(x for x in (user.first_name, user.last_name) if x) or "Unknown")
        handle = f" (@{html.escape(user.username)})" if user.username else ""
        lines.append(f"• <b>{name}</b>{handle} — <code>{user.id}</code> · DM: {'Yes' if user.dm_started else 'No'}")
    return "\n".join(lines) + ("\n\n<i>Showing up to 20 recorded users.</i>" if len(rows) == limit else "")


async def _user_details(session: AsyncSession, prompt: str) -> str:
    base = await _lookup_user(session, prompt)
    if "exact match clear nahi mila" in base or "Kis user ka naam" in base:
        return base
    numeric = re.search(r"(?<!\d)(\d{5,20})(?!\d)", prompt)
    username = re.search(r"(?<!\w)@([A-Za-z0-9_]{5,32})\b", prompt)
    if numeric:
        user = await session.get(User, int(numeric.group(1)))
    elif username:
        user = await session.scalar(select(User).where(func.lower(User.username) == username.group(1).casefold()))
    else:
        cleaned = re.sub(r"(?i)\b(please|alexa|find|search|lookup|look up|who is|tell me|show me|user|member|id|telegram|saved|ki|ka|ke|hai|he|batao|btao|mujhe|iska|iski|naam|name|details|profile|about|full|complete|all)\b", " ", prompt)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" @#:-")
        user = await session.scalar(select(User).where(
            or_(func.lower(User.first_name).contains(cleaned.casefold()),
                func.lower(User.username).contains(cleaned.casefold()))
        ).order_by(User.first_name.asc()))
    if not user:
        return base
    wallet = await session.scalar(select(Wallet).where(Wallet.user_id == user.id))
    economy = await session.scalar(select(Economy).where(Economy.user_id == user.id))
    refs = await session.scalar(select(func.count()).select_from(Referral).where(Referral.referrer_id == user.id))
    memberships = (await session.scalars(
        select(GroupMember).where(GroupMember.user_id == user.id).order_by(GroupMember.joined_at.desc()).limit(10)
    )).all()
    lines = [
        "🪪 <b>Saved user profile</b>",
        f"Name: <b>{html.escape(' '.join(x for x in (user.first_name, user.last_name) if x) or 'Unknown')}</b>",
        f"Username: <b>{'@' + html.escape(user.username) if user.username else 'Not recorded'}</b>",
        f"Telegram ID: <code>{user.id}</code>",
        f"DM started: <b>{'Yes' if user.dm_started else 'No'}</b> · Bot DB banned flag: <b>{'Yes' if user.is_banned else 'No'}</b>",
        f"Telegram Premium flag: <b>{'Yes' if user.is_premium else 'No'}</b>",
        f"Wallet balance: <b>{getattr(wallet, 'balance', 0) or 0}</b> · Bank: <b>{getattr(wallet, 'bank_balance', 0) or 0}</b>",
        f"Level: <b>{getattr(economy, 'level', 1) or 1}</b> · XP: <b>{getattr(economy, 'total_xp', 0) or 0}</b>",
        f"Recorded referrals: <b>{refs or 0}</b>",
        f"Tracked group memberships: <b>{len(memberships)}</b>",
    ]
    if memberships:
        group_ids = [m.group_id for m in memberships]
        groups = (await session.scalars(select(Group).where(Group.id.in_(group_ids)))).all()
        by_id = {g.id: g.title for g in groups}
        lines.extend(["", "<b>Recent tracked groups</b>"])
        lines.extend(f"• {html.escape(by_id.get(m.group_id, str(m.group_id)))} — role: {html.escape(m.role or 'member')}" for m in memberships)
    return "\n".join(lines)


async def _referral_report(session: AsyncSession) -> str:
    total = await session.scalar(select(func.count()).select_from(Referral))
    rewarded = await session.scalar(select(func.count()).select_from(Referral).where(Referral.reward_given.is_(True)))
    pending = await session.scalar(select(func.count()).select_from(AffiliateRequest).where(AffiliateRequest.status == "pending"))
    stars_pending = await session.scalar(select(func.count()).select_from(AffiliateRequest).where(
        AffiliateRequest.status == "pending", AffiliateRequest.request_type == "stars"
    ))
    premium_pending = await session.scalar(select(func.count()).select_from(AffiliateRequest).where(
        AffiliateRequest.status == "pending", AffiliateRequest.request_type == "premium"
    ))
    top = (await session.execute(
        select(User, func.count(Referral.id).label("ref_count"))
        .join(Referral, Referral.referrer_id == User.id)
        .group_by(User.id).order_by(func.count(Referral.id).desc()).limit(5)
    )).all()
    lines = [
        "🎁 <b>Referral / Affiliate overview</b>",
        f"Total recorded referrals: <b>{total or 0}</b>",
        f"Rewards marked given: <b>{rewarded or 0}</b>",
        f"Pending requests: <b>{pending or 0}</b> (Stars: {stars_pending or 0}, Premium: {premium_pending or 0})",
    ]
    if top:
        lines.extend(["", "<b>Top referrers</b>"])
        lines.extend(f"• {html.escape(u.first_name or 'User')} — <code>{u.id}</code> · {count} referrals" for u, count in top)
    return "\n".join(lines)


async def _group_activity_report(message: Message, session: AsyncSession, prompt: str) -> str:
    group = await _find_target_group(message, session, prompt)
    if not group:
        return "Group activity ke liye registered group ka naam ya ID likho."
    stats = await session.scalar(select(GroupStats).where(GroupStats.group_id == group.id))
    total_logs = await session.scalar(select(func.count()).select_from(ActionLog).where(ActionLog.group_id == group.id))
    recent = (await session.scalars(
        select(ActionLog).where(ActionLog.group_id == group.id).order_by(ActionLog.created_at.desc()).limit(8)
    )).all()
    lines = [
        f"📈 <b>Group activity — {html.escape(group.title or str(group.id))}</b>",
        f"Group ID: <code>{group.id}</code>",
        f"Tracked messages: <b>{getattr(stats, 'total_messages', 0) or 0}</b>",
        f"Joins: <b>{getattr(stats, 'total_joins', 0) or 0}</b> · Leaves: <b>{getattr(stats, 'total_leaves', 0) or 0}</b> · Bans: <b>{getattr(stats, 'total_bans', 0) or 0}</b>",
        f"Recorded moderation/activity logs: <b>{total_logs or 0}</b>",
    ]
    if recent:
        lines.extend(["", "<b>Recent recorded actions</b>"])
        lines.extend(f"• {html.escape(log.action)} · {html.escape(log.created_at.strftime('%d %b %H:%M') if log.created_at else 'time unknown')}" for log in recent)
    return "\n".join(lines)


async def _global_controls_report(session: AsyncSession) -> str:
    groups = (await session.scalars(select(Group).where(Group.is_active.is_(True)).order_by(Group.title.asc()))).all()
    anti_spam = anti_flood = lockdown = 0
    for group in groups:
        security = await session.scalar(select(SecuritySettings).where(SecuritySettings.group_id == group.id))
        if security:
            anti_spam += int(bool(security.anti_spam))
            anti_flood += int(bool(security.anti_flood))
            lockdown += int(bool(security.emergency_mode))
    return ("🌐 <b>Global controls status</b>\n"
            f"Active groups: <b>{len(groups)}</b>\n"
            f"Anti-spam ON: <b>{anti_spam}</b> groups\n"
            f"Anti-flood ON: <b>{anti_flood}</b> groups\n"
            f"Emergency lockdown ON: <b>{lockdown}</b> groups\n"
            f"Global Owner AI: <b>{'ON' if await _global_ai_enabled(session) else 'OFF'}</b>")


async def _inbox_report(session: AsyncSession) -> str:
    total = await session.scalar(select(func.count()).select_from(InboxMessage))
    unread = await session.scalar(select(func.count()).select_from(InboxMessage).where(InboxMessage.is_read.is_(False)))
    conversations = await session.scalar(select(func.count(func.distinct(InboxMessage.user_id))))
    return ("📩 <b>Owner inbox overview</b>\n"
            f"Conversations: <b>{conversations or 0}</b>\n"
            f"Saved messages: <b>{total or 0}</b>\nUnread messages: <b>{unread or 0}</b>\n"
            "Use Owner Panel → User Messages to open/reply to individual conversations.")


async def _bot_health_report(session: AsyncSession) -> str:
    users = await session.scalar(select(func.count()).select_from(User))
    dm_users = await session.scalar(select(func.count()).select_from(User).where(User.dm_started.is_(True)))
    groups = await session.scalar(select(func.count()).select_from(Group).where(Group.is_active.is_(True)))
    pending_reviews = await session.scalar(select(func.count()).select_from(ScamReview).where(ScamReview.status == "pending"))
    return ("🩺 <b>Alexa database health snapshot</b>\n"
            f"Recorded users: <b>{users or 0}</b>\nDM starters: <b>{dm_users or 0}</b>\n"
            f"Active groups: <b>{groups or 0}</b>\nPending scam reviews: <b>{pending_reviews or 0}</b>\n"
            "Process uptime/host sleep cannot be verified from this database-only check.")


async def _group_settings_report(message: Message, session: AsyncSession, prompt: str) -> str:
    group = await _find_target_group(message, session, prompt)
    if not group:
        return "Group settings ke liye registered group ka naam ya ID likho."
    sec = await session.scalar(select(SecuritySettings).where(SecuritySettings.group_id == group.id))
    gs = await session.scalar(select(GroupSettings).where(GroupSettings.group_id == group.id))
    fields = [
        ("Anti-spam", "anti_spam"), ("Anti-flood", "anti_flood"), ("Anti-scam", "anti_scam"),
        ("Anti-link", "anti_link"), ("Emergency lockdown", "emergency_mode"),
        ("Lock text", "lock_text"), ("Lock links", "lock_links"), ("Lock photos", "lock_photos"),
        ("Lock videos", "lock_videos"), ("Lock stickers", "lock_stickers"),
    ]
    lines = [f"⚙️ <b>Group settings — {html.escape(group.title or str(group.id))}</b>", f"Group ID: <code>{group.id}</code>"]
    lines.extend(f"• {label}: {'ON' if bool(getattr(sec, field, False)) else 'OFF'}" for label, field in fields)
    lines.append(f"• Silent service messages: {'ON' if bool(getattr(gs, 'silent_service_messages', False)) else 'OFF'}")
    lines.append(f"• Warning limit: {getattr(gs, 'max_warns', 3) if gs else 3} · limit action: {html.escape(getattr(gs, 'warn_action', 'mute') if gs else 'mute')}")
    return "\n".join(lines)


async def _group_status(message: Message, target_group_id: int | None = None, target_group_title: str | None = None) -> str:
    if target_group_id is None and message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return "Group status ke liye registered group ka naam/ID bhi likho."
    group_id = target_group_id or message.chat.id
    group_title = target_group_title or message.chat.title or "this group"
    try:
        member = await message.bot.get_chat_member(group_id, message.bot.id)
        status = str(getattr(member.status, "value", member.status))
        if status == "administrator":
            checks = [
                ("Delete messages", "can_delete_messages"),
                ("Restrict members", "can_restrict_members"),
                ("Invite users", "can_invite_users"),
                ("Pin messages", "can_pin_messages"),
                ("Manage topics", "can_manage_topics"),
            ]
            lines = [f"• {label}: {'✅' if getattr(member, attr, False) else '❌'}" for label, attr in checks]
        elif status == "creator":
            lines = ["• Group creator permissions"]
        else:
            lines = ["• Alexa is not an administrator in this group."]
        return f"🤖 <b>Alexa status — {html.escape(group_title)}</b>\nStatus: <b>{html.escape(status)}</b>\n" + "\n".join(lines)
    except Exception:
        return "Alexa group status check nahi kar saki. Telegram permissions check karo."


async def _set_content_lock(message: Message, session: AsyncSession, action: str, lock_type: str, target_group_id: int | None = None, target_group_title: str | None = None) -> str:
    if target_group_id is None and message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return "Remote lock/unlock ke liye registered group ka exact naam ya ID likho."
    group_id = target_group_id or message.chat.id
    group_title = target_group_title or message.chat.title or "this group"
    try:
        bot_member = await message.bot.get_chat_member(group_id, message.bot.id)
        status = str(getattr(bot_member.status, "value", bot_member.status))
        if status not in {"administrator", "creator"}:
            return "❌ Alexa is group mein admin nahi hai, isliye lock change nahi kar sakti."
        if status == "administrator" and not getattr(bot_member, "can_delete_messages", False):
            return "❌ Alexa ko Delete Messages permission chahiye."
    except Exception:
        return "❌ Alexa ki group permissions verify nahi ho sakin."
    fields = {
        "text": ["lock_text"],
        "links": ["lock_links"],
        "photos": ["lock_photos"],
        "videos": ["lock_videos"],
        "stickers": ["lock_stickers"],
    }
    all_fields = ["lock_text", "lock_links", "lock_photos", "lock_videos", "lock_gifs", "lock_audio",
                  "lock_voice", "lock_documents", "lock_polls", "lock_games", "lock_bots", "lock_forwards",
                  "lock_contacts", "lock_locations", "lock_stickers", "lock_inline", "lock_mentions", "lock_hashtags"]
    security = await session.scalar(select(SecuritySettings).where(SecuritySettings.group_id == group_id))
    if security is None:
        security = SecuritySettings(group_id=group_id)
        session.add(security)
        await session.flush()
    enabled = action == "lock_content"
    targets = all_fields if lock_type == "all" else fields.get(lock_type, [])
    if not targets:
        return "Ye lock type supported nahi. Text, links, photos, videos, stickers ya all try karo."
    for field in targets:
        setattr(security, field, enabled)
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        return "❌ Lock setting save nahi ho saki. Dobara try karo."
    verb = "locked" if enabled else "unlocked"
    return f"✅ <b>{html.escape(lock_type.title())}</b> {verb} in <b>{html.escape(group_title)}</b>."


async def _resolve_target(message: Message, session: AsyncSession, prompt: str, action: str = ""):
    """Resolve explicit target, or the owner for an explicit self-demotion request."""
    if action in {"demote_self", "promote_self"} and message.from_user:
        tg = message.from_user
        target = await session.get(User, tg.id)
        if target is None:
            target = User(id=tg.id, telegram_id=tg.id, username=tg.username, first_name=tg.first_name or "User", last_name=tg.last_name, is_bot=tg.is_bot)
            session.add(target)
            await session.flush()
        return target
    if message.reply_to_message and message.reply_to_message.from_user:
        tg = message.reply_to_message.from_user
        target = await session.get(User, tg.id)
        if target is None:
            target = User(id=tg.id, telegram_id=tg.id, username=tg.username, first_name=tg.first_name or "User",
                          last_name=tg.last_name, is_bot=tg.is_bot)
            session.add(target)
            await session.flush()
        return target
    for entity in (message.entities or []):
        if getattr(entity.type, "value", entity.type) == "text_mention" and entity.user:
            tg = entity.user
            target = await session.get(User, tg.id)
            if target is None:
                target = User(id=tg.id, telegram_id=tg.id, username=tg.username, first_name=tg.first_name or "User",
                              last_name=tg.last_name, is_bot=tg.is_bot)
                session.add(target)
                await session.flush()
            return target
    username_match = re.search(r"(?<![\w])@([A-Za-z0-9_]{5,32})\b", prompt)
    if username_match:
        target = await session.scalar(select(User).where(func.lower(User.username) == username_match.group(1).casefold()))
        if target:
            return target
    id_match = re.search(r"(?<![\d-])(\d{5,20})(?!\d)", prompt)
    if id_match:
        return await session.get(User, int(id_match.group(1)))
    return None


async def _moderation_action(message: Message, session: AsyncSession, action: str, intent: dict, prompt: str, target_group_id: int | None = None, target_group_title: str | None = None) -> str:
    if target_group_id is None and message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return "Remote moderation ke liye registered group ka exact naam ya ID likho."
    group_id = target_group_id or message.chat.id
    group_title = target_group_title or message.chat.title or "this group"
    if action == "make_bot_admin":
        return "Main khud ko admin promote nahi kar sakti. Group ka owner/admin mujhe Telegram ke Admins menu se manually admin banaye; uske baad main apni available permissions ke mutabiq kaam karungi."
    target = await _resolve_target(message, session, prompt, action)
    if not target:
        return "Member ko Telegram mention se select karo, uske message ka reply do, ya uska numeric user ID likho."
    if target.id == message.bot.id:
        return "Main apne aap par ye action nahi laga sakti."
    if target.id == settings.effective_owner_id and action not in {"demote_self", "promote_self"}:
        return "Configured bot owner par moderation action execute nahi kiya gaya, siwaye aapki explicit self-demotion request ke."
    try:
        bot_member = await message.bot.get_chat_member(group_id, message.bot.id)
        bot_status = str(getattr(bot_member.status, "value", bot_member.status))
        target_member = await message.bot.get_chat_member(group_id, target.id)
        target_status = str(getattr(target_member.status, "value", target_member.status))
    except Exception:
        return "Member/group status verify nahi ho saka. Target isi group mein hona chahiye."
    if bot_status != "administrator":
        return "Alexa ko is group mein admin banana hoga."
    needed = {
        "ban": "can_restrict_members", "kick": "can_restrict_members", "mute": "can_restrict_members",
        "unmute": "can_restrict_members", "unban": "can_restrict_members",
        "promote": "can_promote_members", "demote": "can_promote_members", "promote_self": "can_promote_members",
        "warn": "can_delete_messages", "reset_warnings": "can_delete_messages",
    }.get(action)
    if needed and not getattr(bot_member, needed, False):
        permission_name = {"can_restrict_members": "Ban/Restrict Members", "can_promote_members": "Add New Admins",
                           "can_delete_messages": "Delete Messages"}.get(needed, needed)
        return "Alexa ke paas " + permission_name + " permission nahi hai."
    if target_status == "creator" and action in {"ban", "mute", "kick", "promote", "demote", "demote_self", "promote_self"}:
        return "Admin/owner par ye action execute nahi kiya; Telegram admin hierarchy ke rules apply hote hain."
    actor_id = settings.effective_owner_id or 0
    reason = intent.get("reason") or "Owner AI command"
    ok = False
    if action == "ban":
        ok = await ban_user(message.bot, session, group_id, target.id, actor_id, reason)
    elif action == "mute":
        ok = await mute_user(message.bot, session, group_id, target.id, actor_id, int(intent.get("duration_seconds") or 3600), reason)
    elif action == "kick":
        ok = await kick_user(message.bot, session, group_id, target.id, actor_id, reason)
    elif action == "unban":
        ok = await unban_user(message.bot, session, group_id, target.id, actor_id)
    elif action == "unmute":
        ok = await unmute_user(message.bot, session, group_id, target.id, actor_id)
    elif action == "warn":
        count, reached = await warn_user(session, group_id, target.id, actor_id, reason)
        if reached:
            group_settings = await session.scalar(select(GroupSettings).where(GroupSettings.group_id == group_id))
            warn_action = group_settings.warn_action if group_settings else "mute"
            if warn_action == "mute":
                await mute_user(message.bot, session, group_id, target.id, actor_id, 3600, "Warning limit reached")
            elif warn_action == "kick":
                await kick_user(message.bot, session, group_id, target.id, actor_id, "Warning limit reached")
            elif warn_action == "ban":
                await ban_user(message.bot, session, group_id, target.id, actor_id, "Warning limit reached")
            await session.commit()
            return "⚠️ Warning recorded for <a href='tg://user?id=" + str(target.id) + "'>" + html.escape(target.first_name) + "</a>. Count: <b>" + str(count) + "</b>; limit action: <b>" + html.escape(warn_action) + "</b>."
        await session.commit()
        return "⚠️ Warning recorded for <a href='tg://user?id=" + str(target.id) + "'>" + html.escape(target.first_name) + "</a>. Count: <b>" + str(count) + "</b>."
    elif action == "reset_warnings":
        from sqlalchemy import delete
        await session.execute(delete(Warning).where(Warning.group_id == group_id, Warning.user_id == target.id))
        member = await session.scalar(select(GroupMember).where(GroupMember.group_id == group_id, GroupMember.user_id == target.id))
        if member:
            member.warn_count = 0
        await session.commit()
        return "✅ Warnings reset for <a href='tg://user?id=" + str(target.id) + "'>" + html.escape(target.first_name) + "</a>."
    elif action in {"promote", "promote_self", "demote", "demote_self"}:
        try:
            if action == "promote":
                await message.bot.promote_chat_member(
                    chat_id=group_id, user_id=target.id, can_manage_chat=True, can_delete_messages=True,
                    can_manage_video_chats=True, can_restrict_members=True, can_promote_members=False,
                    can_change_info=False, can_invite_users=True, can_pin_messages=True, can_post_messages=False,
                    can_edit_messages=False, can_manage_topics=True,
                )
            else:
                await message.bot.promote_chat_member(
                    chat_id=group_id, user_id=target.id, can_manage_chat=False, can_delete_messages=False,
                    can_manage_video_chats=False, can_restrict_members=False, can_promote_members=False,
                    can_change_info=False, can_invite_users=False, can_pin_messages=False, can_post_messages=False,
                    can_edit_messages=False, can_manage_topics=False,
                )
            verified = await message.bot.get_chat_member(group_id, target.id)
            expected = "administrator" if action in {"promote", "promote_self"} else "member"
            ok = str(getattr(verified.status, "value", verified.status)) == expected
        except Exception:
            ok = False
    if not ok:
        await session.rollback()
        return "❌ " + action.replace("_", " ").title() + " fail hua. Alexa ki permissions aur target ka group status check karo."
    await session.commit()
    verb = {
        "ban": "banned", "mute": "muted for " + str((intent.get("duration_seconds") or 3600) // 60) + " minutes",
        "kick": "kicked", "unban": "unbanned", "unmute": "unmuted",
        "promote": "promoted to admin", "promote_self": "promoted you to admin", "demote": "demoted", "demote_self": "removed from admin",
    }.get(action, action.replace("_", " "))
    return "✅ <b>" + html.escape(target.first_name or "Member") + "</b> " + html.escape(verb) + " successfully in <b>" + html.escape(group_title) + "</b>."


async def _run_request(message: Message, session: AsyncSession, prompt: str) -> str:
    # Resolve common owner-panel data requests deterministically before Gemini.
    # This lets saved user records and reports work even if the model endpoint is unavailable.
    normalized = re.sub(r"[^a-z0-9@ -]+", " ", (prompt or "").casefold())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    has_user_lookup = bool(
        re.search(r"(?<!\w)@[a-z0-9_]{5,32}\b", normalized)
        or re.search(r"(?<![\d-])\d{5,20}(?!\d)", normalized)
    )
    if has_user_lookup and re.search(r"\b(id|user|member|profile|details|name|naam|kaun|who|telegram|referral|wallet|balance|xp|level)\b", normalized):
        if re.search(r"\b(profile|details|full|complete|wallet|balance|xp|level|referral)\b", normalized):
            return await _user_details(session, prompt)
        return await _lookup_user(session, prompt)
    if re.search(r"\b(list|show|all|recent)\b", normalized) and re.search(r"\b(users|members|users list|members list|saved users)\b", normalized):
        return await _users_report(session)
    if re.search(r"\b(referral|referrals|affiliate|withdrawal|withdrawals|stars request|premium request)\b", normalized):
        return await _referral_report(session)
    if re.search(r"\b(inbox|unread messages|user messages|messages from users)\b", normalized):
        return await _inbox_report(session)
    if re.search(r"\b(health|database status|bot status|bot health|is bot running)\b", normalized):
        return await _bot_health_report(session)
    if re.search(r"\b(global controls|anti spam status|anti flood status|lockdown status)\b", normalized):
        return await _global_controls_report(session)
    if re.search(r"\b(group settings|settings for|security settings|which locks|lock status|settings status)\b", normalized):
        return await _group_settings_report(message, session, prompt)
    if re.search(r"\b(group activity|joins and leaves|messages in group|activity report)\b", normalized):
        return await _group_activity_report(message, session, prompt)
    if re.search(r"\b(list|show|all)\b", normalized) and re.search(r"\b(groups|group list|active groups)\b", normalized):
        return await _groups_report(session)
    if re.search(r"\b(today report|daily report|report today|aaj ki report|aaj ka report|global report)\b", normalized):
        return await _today_report(session)
    self_admin_phrases = (
        r"\b(?:mujhe|mujhko|me|myself)\s+(?:group\s+)?admin\s+(?:banao|bana do|make|promote)\b",
        r"\b(?:make|promote)\s+me\s+(?:a\s+)?admin\b",
        r"\b(?:mujhe|mujhko|me|myself)\s+(?:group\s+)?admin\s+se\s+(?:hatao|remove|demote)\b",
        r"\b(?:remove|demote)\s+me\s+(?:from\s+)?admin\b",
    )
    if any(re.search(pattern, normalized) for pattern in self_admin_phrases):
        action = "demote_self" if re.search(r"\b(?:se\s+hatao|remove|demote)\b", normalized) else "promote_self"
        intent = {"action": action, "type": "", "duration_seconds": 3600, "reason": "Owner self-admin request"}
        target_group = await _find_target_group(message, session, prompt)
        if target_group is None:
            return "Apna admin role change karne ke liye target registered group ka naam/ID likho."
        return await _moderation_action(message, session, action, intent, prompt, target_group.id, target_group.title)
    try:
        intent = await _classify_request(prompt)
    except Exception as exc:
        logger.warning("Owner AI request classification failed: %s", type(exc).__name__)
        lookup_hints = ("telegram id", " id", "username", "who is", "who's", "kaun hai", "profile", "details", "member", "user", "naam", "name", "ki id", "ka id", "add members")
        if "@" in prompt or any(hint in prompt.casefold() for hint in lookup_hints):
            return await _lookup_user(session, prompt)
        action_words = r"\b(ban|mute|kick|promote|demote|unban|unmute|warn|lock|unlock|admin|hatao|karo|kar do|remove|restrict)\b"
        if re.search(action_words, normalized):
            return "Owner action classify nahi ho saka, isliye kuch execute nahi hua. Request ko target user aur group ke saath dobara likho."
        return await _answer_owner_question(prompt)
    action = intent["action"]
    if action == "unsupported":
        lowered_prompt = prompt.casefold()
        lookup_hints = ("telegram id", " id", "username", "who is", "who's", "kaun hai", "profile", "details", "member", "user", "naam", "name", "ki id", "ka id")
        if "@" in prompt or any(hint in lowered_prompt for hint in lookup_hints):
            return await _lookup_user(session, prompt)
    if action == "report_today":
        return await _today_report(session)
    if action == "list_groups":
        return await _groups_report(session)
    if action == "list_users":
        return await _users_report(session)
    if action == "lookup_user":
        return await _lookup_user(session, prompt)
    if action == "user_details":
        return await _user_details(session, prompt)
    if action == "referral_report":
        return await _referral_report(session)
    if action == "group_activity":
        return await _group_activity_report(message, session, prompt)
    if action == "bot_health":
        return await _bot_health_report(session)
    if action == "global_controls_status":
        return await _global_controls_report(session)
    if action == "inbox_summary":
        return await _inbox_report(session)
    if action == "group_settings_status":
        return await _group_settings_report(message, session, prompt)
    if action in {"group_status", "lock_content", "unlock_content", "ban", "mute", "kick", "promote", "promote_self", "demote", "demote_self", "make_bot_admin", "unban", "unmute", "warn", "reset_warnings"}:
        target_group = await _find_target_group(message, session, prompt)
        if target_group is None:
            return "Remote action ke liye registered group ka exact naam ya numeric group ID likho. Example: <code>@user ko mute karo in My Group</code>."
        if action == "group_status":
            return await _group_status(message, target_group.id, target_group.title)
        if action in {"lock_content", "unlock_content"}:
            return await _set_content_lock(message, session, action, intent["type"], target_group.id, target_group.title)
        return await _moderation_action(message, session, action, intent, prompt, target_group.id, target_group.title)
    if action == "help":
        return (
            "👑 <b>Owner AI Control</b>\n"
            "• General knowledge aur normal questions ka jawab\n"
            "• Saved users se name/@username/Telegram ID lookup\n"
            "• Aaj ki report, saved user list/profile, referral/Stars overview, inbox counts, bot/database snapshot\n"
            "• Active groups, group activity, global controls status, per-group security settings/locks\n"
            "• Registered target group ka status/locks\n"
            "• Target group mein ban, mute, kick, promote, demote, unban, unmute, warn aur warning reset\n"
            "Remote actions ke liye group ka naam/ID aur member ka @username, ID, ya group message ka reply zaroori hai. "
            "Har action se pehle Alexa admin status aur permissions verify karti hai."
        )
    action_words = r"\b(ban|mute|kick|promote|demote|unban|unmute|warn|lock|unlock|admin|hatao|remove|restrict)\b"
    if re.search(action_words, normalized):
        return "Is specific bot action ka safe handler abhi connected nahi, isliye maine kuch execute nahi kiya."
    return await _answer_owner_question(prompt)


def _ai_control_kb(enabled: bool):
    builder = InlineKeyboardBuilder()
    builder.button(text=("🟢 Global AI: ON" if enabled else "🔴 Global AI: OFF"), callback_data="owner:ai_toggle")
    builder.button(text="💬 Ask Alexa / Run Request", callback_data="owner:ai_ask")
    builder.button(text="👑 Owner Panel", callback_data="owner:menu")
    builder.adjust(1, 1, 1)
    return builder.as_markup()


@router.callback_query(F.data == "owner:ai")
async def owner_ai_panel(callback: CallbackQuery, db_session: AsyncSession):
    if not _is_owner(callback.from_user.id):
        await callback.answer("Owner only.", show_alert=True)
        return
    enabled = await _global_ai_enabled(db_session)
    await callback.message.answer(
        "🧠 <b>Alexa Global AI Control</b>\n\n"
        f"Status: <b>{'ON' if enabled else 'OFF'}</b>\n"
        "ON: owner requests DM ya kisi bhi chat se registered target group par run ho sakti hain, "
        "jab Alexa us group mein admin ho aur required permission milti ho. Normal members ki casual AI chat alag rahegi.\n"
        "OFF: owner AI commands band; normal AI chat aur baqi bot features apni settings ke mutabiq chalte rahenge.\n\n"
        "User lookup sirf Alexa ke saved/recorded users se hoga—Telegram ki private/global directory ka access nahi hota.",
        parse_mode="HTML", reply_markup=_ai_control_kb(enabled),
    )
    await callback.answer()


@router.callback_query(F.data == "owner:ai_toggle")
async def owner_ai_toggle(callback: CallbackQuery, db_session: AsyncSession):
    if not _is_owner(callback.from_user.id):
        await callback.answer("Owner only.", show_alert=True)
        return
    enabled = not await _global_ai_enabled(db_session)
    await _set_global_ai_enabled(db_session, enabled)
    await callback.message.edit_text(
        "🧠 <b>Alexa Global AI Control</b>\n\n"
        f"Status: <b>{'ON 🟢' if enabled else 'OFF 🔴'}</b>\n"
        "ON par owner global request aur target group actions use kar sakta hai. OFF par owner AI control band rahega; "
        "normal AI chat aur doosre bot features unchanged rahenge.\n\n"
        "User details Alexa ke saved records se milengi, na ke Telegram ki private/global directory se.",
        parse_mode="HTML", reply_markup=_ai_control_kb(enabled),
    )
    await callback.answer("Global AI " + ("ON" if enabled else "OFF"))


@router.callback_query(F.data == "owner:ai_ask")
async def owner_ai_ask(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession):
    if not _is_owner(callback.from_user.id):
        await callback.answer("Owner only.", show_alert=True)
        return
    if not await _global_ai_enabled(db_session):
        await callback.answer("Pehle Global AI ON karo.", show_alert=True)
        return
    await state.set_state(OwnerAIState.waiting_prompt)
    await callback.message.answer(
        "🧠 <b>Alexa ready hai.</b> Apni request normal Roman Urdu/English mein likho.\n\n"
        "Examples:\n"
        "• <code>Ali ka saved Telegram ID batao</code>\n"
        "• <code>Active groups ki list do</code>\n"
        "• <code>@user ko mute karo in My Group</code>\n"
        "• <code>My Group mein links lock karo</code>\n\n"
        "Remote action mein registered group ka naam ya ID zaroor likhna. /cancel se close karo.",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(F.chat.type == ChatType.PRIVATE, F.text, OwnerAIState.waiting_prompt)
async def owner_ai_panel_prompt(message: Message, state: FSMContext, db_session: AsyncSession, **kwargs):
    if not _is_owner(message.from_user.id):
        await state.clear()
        return
    prompt = (message.text or "").strip()
    if prompt.lower() in {"/cancel", "cancel", "close"}:
        await state.clear()
        await message.answer("AI Control session closed.")
        return
    if not await _global_ai_enabled(db_session):
        await state.clear()
        await message.answer("Global AI Control OFF hai. Owner Panel → Global AI Control se ON karo.")
        return
    status = await message.answer("🧠 Request check kar rahi hoon…")
    result = await _run_request(message, db_session, prompt)
    await state.clear()
    try:
        await status.edit_text(result, parse_mode="HTML")
    except Exception:
        await message.answer(result, parse_mode="HTML")


async def is_owner_ai_message(message: Message) -> bool:
    return bool(
        message.from_user
        and _is_owner(message.from_user.id)
        and message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)
        and await _explicitly_addressed(message)
    )


async def is_owner_direct_ai_message(message: Message) -> bool:
    text = (message.text or "").strip()
    return bool(
        message.from_user
        and _is_owner(message.from_user.id)
        and message.chat.type == ChatType.PRIVATE
        and text
        and not text.startswith("/")
    )


@router.message(F.chat.type == ChatType.PRIVATE, F.text, is_owner_direct_ai_message)
async def owner_ai_direct_dm(message: Message, db_session: AsyncSession, **kwargs):
    # When Global AI is ON, owner can ask Alexa from a plain DM without opening the panel first.
    # When OFF, pass the message to normal casual AI chat instead.
    if not await _global_ai_enabled(db_session):
        from bot.handlers.ai_chat import alexa_ai_chat
        await alexa_ai_chat(message)
        return
    result = await _run_request(message, db_session, (message.text or "").strip())
    await message.answer(result, parse_mode="HTML", disable_web_page_preview=True)


@router.message(
    F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
    F.text,
    is_owner_ai_message,
)
async def owner_ai_group_prompt(message: Message, db_session: AsyncSession, **kwargs):
    if not await _global_ai_enabled(db_session):
        # Global control is OFF, but owner can still use Alexa for ordinary AI chat.
        from bot.handlers.ai_chat import alexa_ai_chat
        await alexa_ai_chat(message)
        return
    prompt = _strip_prefix(message.text or "")
    if not prompt and message.reply_to_message:
        prompt = (message.reply_to_message.text or "").strip()
    if not prompt:
        await message.reply("Bolo, kya karna hai?")
        return
    result = await _run_request(message, db_session, prompt)
    await message.reply(result, parse_mode="HTML", disable_web_page_preview=True)
