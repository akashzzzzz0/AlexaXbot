"""Group-admin scam review queue; does not automatically punish reported users."""
from html import escape

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.services.scam_review_service import (
    list_pending_reviews, resolve_scam_review, submit_scam_review,
)

router = Router()


async def _is_admin(message: Message) -> bool:
    if message.chat.type not in ("group", "supergroup") or not message.from_user:
        return False
    try:
        member = await message.bot.get_chat_member(message.chat.id, message.from_user.id)
        status = str(getattr(member.status, "value", member.status))
        return status in ("administrator", "creator")
    except Exception:
        return False


@router.message(Command("scamreview"))
async def scam_review(message: Message, db_session: AsyncSession, **kwargs):
    if not await _is_admin(message):
        await message.reply("🔒 Only group admins can submit scam reviews.")
        return
    target = message.reply_to_message.from_user if message.reply_to_message else None
    if not target or target.is_bot:
        await message.reply("Reply to the suspicious user's message with /scamreview reason.")
        return
    reason = (message.text or "").partition(" ")[2].strip()
    if len(reason) < 8:
        await message.reply("Add a clear reason (at least 8 characters), e.g. /scamreview impersonation and payment scam.")
        return
    evidence = (message.reply_to_message.text or message.reply_to_message.caption or "")[:1000]
    try:
        item = await submit_scam_review(
            db_session, group_id=message.chat.id, target_user_id=target.id,
            reporter_user_id=message.from_user.id, reason=reason, evidence=evidence,
        )
        await db_session.commit()
    except ValueError as exc:
        await db_session.rollback()
        await message.reply(escape(str(exc)))
        return
    await message.reply(
        f"🛡️ <b>Scam review #{item.id} queued</b>\n"
        f"Target: <code>{target.id}</code>\n"
        "This is an admin review item, not proof of wrongdoing. No automatic ban was applied."
    )


@router.message(Command("scamreviews"))
async def scam_reviews(message: Message, db_session: AsyncSession, **kwargs):
    if not await _is_admin(message):
        await message.reply("🔒 Only group admins can view scam reviews.")
        return
    items = await list_pending_reviews(db_session, message.chat.id)
    if not items:
        await message.reply("✅ No pending scam reviews in this group.")
        return
    lines = ["🛡️ <b>Pending Scam Reviews</b>"]
    for item in items:
        lines.append(
            f"\n<b>#{item.id}</b> · Target <code>{item.target_user_id}</code>\n"
            f"Reason: {escape(item.reason[:180])}\n"
            f"Evidence: {escape(item.evidence[:180] or 'No text evidence saved')}"
        )
    lines.append("\nResolve an item with <code>/scamresolve ID optional note</code>.")
    await message.reply("\n".join(lines))


@router.message(Command("scamresolve"))
async def scam_resolve(message: Message, db_session: AsyncSession, **kwargs):
    if not await _is_admin(message):
        await message.reply("🔒 Only group admins can resolve scam reviews.")
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 2 or not parts[1].isdigit():
        await message.reply("Usage: /scamresolve ID optional note")
        return
    note = parts[2] if len(parts) > 2 else ""
    resolved = await resolve_scam_review(
        db_session, review_id=int(parts[1]), group_id=message.chat.id,
        resolver_user_id=message.from_user.id, note=note,
    )
    if not resolved:
        await message.reply("Review not found in this group, or it was already resolved.")
        return
    await db_session.commit()
    await message.reply(f"✅ Scam review #{int(parts[1])} marked as reviewed. No automatic moderation was performed.")
