"""Opt-in Community Shield commands. No automatic bans or cross-group actions."""
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services.community_shield_service import (
    COMMUNITY_FLAG_THRESHOLD,
    group_is_enabled,
    report_summary,
    report_user,
    set_group_enabled,
)

router = Router()


async def _is_group_admin(message: Message) -> bool:
    if message.chat.type not in ("group", "supergroup") or not message.from_user:
        return False
    try:
        member = await message.bot.get_chat_member(message.chat.id, message.from_user.id)
        status = str(getattr(member.status, "value", member.status))
        return status in ("administrator", "creator")
    except Exception:
        return False


async def _is_reply_to_user(message: Message) -> bool:
    return bool(
        message.reply_to_message
        and message.reply_to_message.from_user
        and not message.reply_to_message.from_user.is_bot
    )


@router.message(Command("shield"))
async def shield_status_or_toggle(message: Message, db_session: AsyncSession, **kwargs):
    if not await _is_group_admin(message):
        await message.reply("🔒 Community Shield settings are available to group admins only.")
        return
    parts = (message.text or "").split()
    if len(parts) > 1:
        choice = parts[1].lower()
        if choice not in {"on", "off"}:
            await message.reply("Usage: /shield on, /shield off, or /shield")
            return
        enabled = await set_group_enabled(db_session, message.chat.id, choice == "on")
        await db_session.commit()
        await message.reply(
            "🛡️ Community Shield is now <b>ON</b> for this group. Admins can submit and check reports."
            if enabled else
            "⚪ Community Shield is now <b>OFF</b> for this group. Its reports no longer count toward the shared threshold."
        )
        return
    enabled = await group_is_enabled(db_session, message.chat.id)
    state = "ON" if enabled else "OFF"
    await message.reply(
        f"🛡️ <b>Community Shield: {state}</b>\n\n"
        "Opt-in cross-group scam reports with human review. No automatic bans.\n"
        "• <code>/shield on</code> — join the network\n"
        "• <code>/shield off</code> — leave the network\n"
        "• Reply to a user's message with <code>/shieldreport reason</code>\n"
        "• Reply to a user's message with <code>/shieldcheck</code>"
    )


@router.message(Command("shieldreport"))
async def shield_report_command(message: Message, db_session: AsyncSession, **kwargs):
    if not await _is_group_admin(message):
        await message.reply("🔒 Only group admins can submit Community Shield reports.")
        return
    if not await group_is_enabled(db_session, message.chat.id):
        await message.reply("Enable the network first with /shield on.")
        return
    if not await _is_reply_to_user(message):
        await message.reply("Reply to the user's message: /shieldreport reason")
        return
    reason = (message.text or "").partition(" ")[2].strip()
    if len(reason) < 8:
        await message.reply("Add a brief evidence-based reason (at least 8 characters). Avoid guesses or personal disputes.")
        return
    target_id = message.reply_to_message.from_user.id
    if target_id == message.from_user.id:
        await message.reply("You cannot report yourself.")
        return
    try:
        created = await report_user(
            db_session,
            group_id=message.chat.id,
            target_user_id=target_id,
            reporter_user_id=message.from_user.id,
            reason=reason,
        )
        await db_session.commit()
    except ValueError as exc:
        await message.reply(str(exc))
        return
    if not created:
        await message.reply("This group has already submitted a report for that user. Duplicate reports do not increase the count.")
        return
    count, flagged = await report_summary(db_session, target_id)
    status = (
        f"⚠️ Community threshold reached ({count} participating groups). An admin should review the evidence."
        if flagged else
        f"🧾 Report recorded. Independent participating groups: {count}/{COMMUNITY_FLAG_THRESHOLD}. No automatic action was taken."
    )
    await message.reply(
        f"🛡️ <b>Community Shield report saved</b>\n"
        f"User ID: <code>{target_id}</code>\n"
        f"Reason: {reason[:500]}\n\n{status}"
    )


@router.message(Command("shieldcheck"))
async def shield_check_command(message: Message, db_session: AsyncSession, **kwargs):
    if not await _is_group_admin(message):
        await message.reply("🔒 Only group admins can check Community Shield reports.")
        return
    if not await group_is_enabled(db_session, message.chat.id):
        await message.reply("Enable the network first with /shield on.")
        return
    if not await _is_reply_to_user(message):
        await message.reply("Reply to a user's message with /shieldcheck.")
        return
    target_id = message.reply_to_message.from_user.id
    count, flagged = await report_summary(db_session, target_id)
    if flagged:
        text = (
            f"⚠️ <b>Community reports threshold reached</b>\n"
            f"User ID: <code>{target_id}</code>\n"
            f"Reports from {count} participating groups. This is a signal for human review, not proof of wrongdoing."
        )
    elif count:
        text = (
            f"🧾 <b>Community Shield result</b>\n"
            f"User ID: <code>{target_id}</code>\n"
            f"Reports from {count}/{COMMUNITY_FLAG_THRESHOLD} participating groups. Not enough reports to reach the review threshold."
        )
    else:
        text = f"✅ No active Community Shield reports found for user ID <code>{target_id}</code>."
    await message.reply(text)
