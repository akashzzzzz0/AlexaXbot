"""Owner-only global intelligence reports and safe feature-wiring audit.

This module is intentionally additive: it does not alter moderation handlers,
settings, database schemas, or existing owner workflows. It handles only two
read-only request families and lets every other message continue to the
existing owner AI router.
"""
import html
import logging
import re
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.enums import ChatType
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.database.models import (
    ActionLog,
    DailyGroupMessageStat,
    Group,
    GroupMemberJoin,
    User,
)

router = Router(name="owner_global_intelligence")
logger = logging.getLogger(__name__)
LOCAL_TZ = ZoneInfo("Asia/Kolkata")

# These are feature routers already used by the existing application. The audit
# checks registration/wiring, not whether every possible live Telegram scenario
# has been exercised.
FEATURE_ROUTERS = (
    ("Moderation commands", "moderation"),
    ("Security controls", "security"),
    ("Economy and wallet", "economy"),
    ("Games", "games"),
    ("Alexa games", "alexa_games"),
    ("World events", "world_events"),
    ("Referrals", "affiliate"),
    ("Group promotion", "group_promotion"),
    ("Welcome and join flow", "welcome"),
    ("Owner panel", "owner"),
    ("Owner AI Commander", "owner_ai_control"),
    ("Member AI chat", "ai_chat"),
    ("Statistics", "statistics"),
    ("Group tools", "group_tools"),
    ("Community Shield", "community_shield"),
    ("Scam review", "scam_review"),
    ("VIP features", "ultra_vip"),
    ("Owner group inbox", "owner_group_message"),
    ("Owner group activity", "owner_group_activity"),
)


def normalize_request(text: str) -> str:
    """Normalize mixed Roman Urdu/English text for deterministic report routing."""
    return re.sub(r"\\s+", " ", (text or "").casefold()).strip()


def is_audit_request(text: str) -> bool:
    value = normalize_request(text)
    patterns = (
        r"\\b(audit|self[- ]?audit|health check|feature check|features check|feature status|feature report)\\b",
        r"\\b(konsa|kon sa|kaun sa|which|what)\\b.{0,70}\\b(feature|features|system|module)\\b.{0,50}\\b(working|work|kaam|failed|fail|issue|problem|status)\\b",
        r"\\b(feature|features|module|modules)\\b.{0,60}\\b(working|work|kaam|fail|failed|check|status|issue|problem)\\b",
        r"\\b(bot|alexa|system)\\b.{0,50}\\b(check|audit|test|scan)\\b.{0,50}\\b(feature|features|system|modules)\\b",
        r"\\b(complete|full|poora|pura)\\b.{0,30}\\b(bot|system|feature)\\b.{0,30}\\b(report|audit|check)\\b",
    )
    return any(re.search(pattern, value) for pattern in patterns)


def is_24h_report_request(text: str) -> bool:
    value = normalize_request(text)
    has_window = bool(re.search(
        r"\\b(24\\s*(hours?|hrs?|ghante|ghanta)|last\\s*day|past\\s*day|pichle\\s*24|guzre\\s*24|aakhri\\s*24)\\b",
        value,
    ))
    has_report = bool(re.search(r"\\b(report|haal|stats|statistics|activity|summary)\\b", value))
    return has_window and has_report


def _is_owner(user_id: int | None) -> bool:
    return bool(user_id and settings.effective_owner_id and user_id == settings.effective_owner_id)


def _owner_addressed(message: Message) -> bool:
    text = message.text or ""
    if re.match(r"^\\s*(?:alexa\\b|@alexasuper_bot\\b)", text, re.I):
        return True
    reply = message.reply_to_message
    return bool(reply and reply.from_user and reply.from_user.id == message.bot.id)


def _strip_address(text: str) -> str:
    return re.sub(r"^\\s*(?:alexa\\b|@alexasuper_bot\\b)[\\s,!:.-]*", "", text or "", flags=re.I).strip()


async def _count(session: AsyncSession, statement) -> int:
    value = await session.scalar(statement)
    return int(value or 0)


async def build_24h_report(session: AsyncSession) -> str:
    """Report only metrics with timestamps precise enough for a rolling 24h window."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)
    new_users = await _count(
        session,
        select(func.count(User.id)).where(User.created_at >= cutoff, User.created_at <= now),
    )
    active_users = await _count(
        session,
        select(func.count(User.id)).where(User.last_seen >= cutoff, User.last_seen <= now),
    )
    new_groups = await _count(
        session,
        select(func.count(Group.id)).where(Group.created_at >= cutoff, Group.created_at <= now),
    )
    joins = await _count(
        session,
        select(func.count(GroupMemberJoin.id)).where(
            GroupMemberJoin.joined_at >= cutoff, GroupMemberJoin.joined_at <= now
        ),
    )
    actions = await _count(
        session,
        select(func.count(ActionLog.id)).where(
            ActionLog.created_at >= cutoff, ActionLog.created_at <= now
        ),
    )
    breakdown = (await session.execute(
        select(ActionLog.action, func.count(ActionLog.id))
        .where(ActionLog.created_at >= cutoff, ActionLog.created_at <= now)
        .group_by(ActionLog.action)
        .order_by(func.count(ActionLog.id).desc())
        .limit(8)
    )).all()

    local_now = now.astimezone(LOCAL_TZ)
    today_key = local_now.date().isoformat()
    today_messages = await _count(
        session,
        select(func.coalesce(func.sum(DailyGroupMessageStat.message_count), 0)).where(
            DailyGroupMessageStat.day_key == today_key
        ),
    )

    lines = [
        "📊 <b>ALEXA — Last 24 Hours Report</b>",
        f"🕒 Window: <b>{(now - timedelta(hours=24)).strftime('%d %b %H:%M')} – {now.strftime('%d %b %H:%M')} UTC</b>",
        "",
        f"👤 New registered users: <b>{new_users}</b>",
        f"🟢 Users seen in last 24h: <b>{active_users}</b>",
        f"🌐 Newly registered groups: <b>{new_groups}</b>",
        f"👥 Recorded group joins: <b>{joins}</b>",
        f"🛡️ Recorded moderation/actions: <b>{actions}</b>",
    ]
    if breakdown:
        lines.extend(["", "<b>Action breakdown</b>"])
        lines.extend(
            f"• {html.escape(str(action or 'unknown'))}: <b>{int(count or 0)}</b>"
            for action, count in breakdown
        )
    lines.extend([
        "",
        f"💬 Messages recorded today (India date): <b>{today_messages}</b>",
        "<i>Note: the existing message counter stores calendar-day totals, not per-message timestamps, so this is not presented as an exact rolling 24h message count.</i>",
    ])
    return "\\n".join(lines)


def _router_registered(module_name: str) -> tuple[bool, str]:
    module = sys.modules.get(f"bot.handlers.{module_name}")
    if module is None:
        return False, "module not loaded"
    feature_router = getattr(module, "router", None)
    if feature_router is None:
        return False, "router object missing"
    if getattr(feature_router, "parent_router", None) is None:
        return False, "router not attached to main application"
    return True, "router attached"


async def build_feature_audit(message: Message, session: AsyncSession) -> str:
    """Run non-destructive runtime/wiring checks; never claim untested logic works."""
    checks: list[tuple[str, bool, str]] = []

    try:
        await session.execute(select(1))
        checks.append(("Database read", True, "query succeeded"))
    except Exception as exc:
        logger.warning("Owner feature audit database check failed: %s", type(exc).__name__)
        checks.append(("Database read", False, "query failed"))

    try:
        me = await message.bot.get_me()
        checks.append(("Telegram Bot API", True, f"connected as @{html.escape(me.username or me.first_name)}"))
    except Exception as exc:
        logger.warning("Owner feature audit Telegram check failed: %s", type(exc).__name__)
        checks.append(("Telegram Bot API", False, "getMe request failed"))

    checks.append((
        "Configured numeric Owner ID",
        bool(settings.effective_owner_id),
        "configured" if settings.effective_owner_id else "BOT_OWNER_ID/OWNER_ID missing",
    ))

    registered = 0
    for label, module_name in FEATURE_ROUTERS:
        ok, detail = _router_registered(module_name)
        registered += int(ok)
        checks.append((label, ok, detail))

    # Anti-Link/Forward is an outer middleware rather than a feature router.
    anti_link_module = sys.modules.get("bot.middlewares.anti_link")
    middleware_loaded = bool(
        anti_link_module and getattr(anti_link_module, "AntiLinkEnforcementMiddleware", None)
    )
    checks.append((
        "Anti-Link / Anti-Forward middleware code",
        middleware_loaded,
        "middleware class loaded; message enforcement is not simulated by this read-only audit"
        if middleware_loaded else "middleware module/class not loaded",
    ))

    active_groups = await _count(
        session, select(func.count(Group.id)).where(Group.is_active.is_(True))
    )
    sample_groups = (await session.scalars(
        select(Group).where(Group.is_active.is_(True)).order_by(Group.title.asc()).limit(12)
    )).all()
    admin_count = delete_count = restrict_count = unknown_count = 0
    for group in sample_groups:
        try:
            member = await message.bot.get_chat_member(group.id, message.bot.id)
            status = str(getattr(member.status, "value", member.status))
            if status in {"administrator", "creator"}:
                admin_count += 1
                delete_count += int(bool(getattr(member, "can_delete_messages", False)))
                restrict_count += int(bool(getattr(member, "can_restrict_members", False)))
            else:
                unknown_count += 1
        except Exception:
            unknown_count += 1

    passed = sum(1 for _, ok, _ in checks if ok)
    failed = len(checks) - passed
    lines = [
        "🩺 <b>ALEXA — Safe Self-Audit</b>",
        f"🧩 Feature routers attached: <b>{registered}/{len(FEATURE_ROUTERS)}</b>",
        f"🟢 Checks passed: <b>{passed}</b> · 🔴 Checks failed: <b>{failed}</b>",
        "",
        "<b>Core runtime checks</b>",
    ]
    for label, ok, detail in checks[:3]:
        icon = "🟢" if ok else "🔴"
        lines.append(f"{icon} {html.escape(label)}: <b>{html.escape(detail)}</b>")

    lines.extend(["", "<b>Feature registration checks</b>"])
    for label, ok, detail in checks[3:]:
        icon = "🟢" if ok else "🔴"
        lines.append(f"{icon} {html.escape(label)} — {html.escape(detail)}")

    lines.extend([
        "",
        "<b>Telegram group permission probe</b>",
        f"Active registered groups: <b>{active_groups}</b>",
        f"Read-only sample checked: <b>{len(sample_groups)}</b> (maximum 12)",
        f"Alexa admin/creator: <b>{admin_count}</b>",
        f"Delete Messages permission: <b>{delete_count}</b>",
        f"Restrict Members permission: <b>{restrict_count}</b>",
        f"Could not verify / not admin: <b>{unknown_count}</b>",
        "",
        "⚠️ <b>Important:</b> Green means the module/router or safe connectivity check is present—not that every feature has passed a real end-to-end test. Ban, mute, delete, locks, payments, and other live actions are never executed during this audit.",
    ])
    return "\\n".join(lines)


@router.message(F.text)
async def owner_global_intelligence_dispatch(
    message: Message,
    db_session: AsyncSession,
    state: FSMContext,
    **kwargs,
):
    """Intercept only owner audit/report prompts; pass all other traffic onward."""
    if not message.from_user or not _is_owner(message.from_user.id):
        raise SkipHandler
    if await state.get_state():
        raise SkipHandler
    if message.text.startswith("/"):
        raise SkipHandler

    if message.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}:
        if not _owner_addressed(message):
            raise SkipHandler
        prompt = _strip_address(message.text)
    elif message.chat.type == ChatType.PRIVATE:
        prompt = _strip_address(message.text)
    else:
        raise SkipHandler

    if is_audit_request(prompt):
        response = await build_feature_audit(message, db_session)
    elif is_24h_report_request(prompt):
        try:
            response = await build_24h_report(db_session)
        except Exception as exc:
            logger.exception("Owner 24h report failed")
            await db_session.rollback()
            response = (
                "❌ <b>24-hour report complete nahi ho saki.</b> "
                f"Database query failed: <code>{html.escape(type(exc).__name__)}</code>"
            )
    else:
        raise SkipHandler

    await message.answer(response, parse_mode="HTML", disable_web_page_preview=True)
