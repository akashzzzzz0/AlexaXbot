"""Twice-daily ALEXA Free Group Promotion announcement scheduler.

This is separate from member-submitted cross-group promotion posts. It announces
ALEXA's own free-promotion flow to every active group where the bot is still a
member, without requiring the group's /promotions opt-in.
"""
import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select

from bot.config import settings
from bot.database.connection import async_session_maker
from bot.database.models import GlobalSetting, Group

logger = logging.getLogger(__name__)

_TIMEZONE = ZoneInfo("Asia/Karachi")
_SLOTS = ((10, "morning"), (19, "evening"))
_SLOT_WINDOW_MINUTES = 30
_CHECK_INTERVAL_SECONDS = 20
_scheduler_task: asyncio.Task | None = None


def _sent_key(group_id: int, slot_name: str) -> str:
    # GlobalSetting.key is limited to 64 characters.
    return f"alexa_daily_promo:{group_id}:{slot_name}"


def _announcement_markup(bot_username: str):
    builder = InlineKeyboardBuilder()
    builder.button(
        text="➕ ADD ALEXA TO GROUP",
        url=f"https://t.me/{bot_username}?startgroup=true",
    )
    builder.button(
        text="💬 START ALEXA IN DM",
        url=f"https://t.me/{bot_username}?start=group_promotion",
    )
    owner_id = settings.effective_owner_id
    if owner_id:
        builder.button(
            text="👑 CONTACT OWNER",
            url=f"tg://user?id={owner_id}",
        )
    builder.adjust(1)
    return builder.as_markup()


_ANNOUNCEMENT_TEXT = (
    "💜 <b>FREE GROUP PROMOTION</b> 🚀\n\n"
    "Apne group ko Alexa ke through FREE promote karwayein!\n\n"
    "<b>Just 2 simple steps:</b>\n"
    "01 ➕ Add Alexa to your group\n"
    "02 💬 Send your group link in Alexa's DM\n\n"
    "✨ <b>100% FREE · No Promotion Fee</b>"
)


async def _already_sent(group_id: int, slot_name: str, day_key: str) -> bool:
    async with async_session_maker() as session:
        row = await session.get(GlobalSetting, _sent_key(group_id, slot_name))
        return bool(row and row.value == day_key)


async def _record_sent(group_id: int, slot_name: str, day_key: str) -> None:
    async with async_session_maker() as session:
        key = _sent_key(group_id, slot_name)
        row = await session.get(GlobalSetting, key)
        if row is None:
            session.add(GlobalSetting(key=key, value=day_key))
        else:
            row.value = day_key
        await session.commit()


async def _send_to_active_groups(bot, slot_name: str, day_key: str) -> None:
    from bot.handlers.owner import get_maintenance_mode
    if get_maintenance_mode():
        return
    async with async_session_maker() as session:
        targets = list((await session.scalars(
            select(Group.id).where(Group.is_active.is_(True))
        )).all())

    me = await bot.get_me()
    if not me.username:
        logger.warning("Daily group promotion skipped: bot username is unavailable")
        return
    markup = _announcement_markup(me.username)

    sent = 0
    skipped = 0
    failed = 0
    for group_id in targets:
        try:
            if get_maintenance_mode():
                break
            if await _already_sent(group_id, slot_name, day_key):
                skipped += 1
                continue

            # The database can contain stale group rows. Confirm Alexa is still
            # a member before sending; this does not require Alexa to be admin.
            member = await bot.get_chat_member(group_id, me.id)
            status = str(getattr(member.status, "value", member.status)).lower()
            if status not in {"member", "administrator", "creator"}:
                skipped += 1
                continue

            try:
                await bot.send_message(
                    group_id,
                    _ANNOUNCEMENT_TEXT,
                    parse_mode="HTML",
                    reply_markup=markup,
                    disable_web_page_preview=True,
                )
            except TelegramRetryAfter as exc:
                await asyncio.sleep(max(1, int(exc.retry_after)))
                await bot.send_message(
                    group_id,
                    _ANNOUNCEMENT_TEXT,
                    parse_mode="HTML",
                    reply_markup=markup,
                    disable_web_page_preview=True,
                )

            # Persist per group and per daily slot so restarts do not resend it.
            await _record_sent(group_id, slot_name, day_key)
            sent += 1
            await asyncio.sleep(0.08)
        except TelegramForbiddenError:
            skipped += 1
        except asyncio.CancelledError:
            raise
        except Exception:
            failed += 1
            logger.exception(
                "Daily ALEXA promotion failed for group_id=%s slot=%s",
                group_id,
                slot_name,
            )

    logger.info(
        "Daily ALEXA group promotion slot completed",
        extra={
            "slot": slot_name,
            "day": day_key,
            "sent": sent,
            "skipped": skipped,
            "failed": failed,
        },
    )


async def _daily_promotion_scheduler(bot) -> None:
    while True:
        try:
            from bot.handlers.owner import get_maintenance_mode
            if get_maintenance_mode():
                await asyncio.sleep(_CHECK_INTERVAL_SECONDS)
                continue
            now = datetime.now(_TIMEZONE)
            day_key = now.date().isoformat()
            for hour, slot_name in _SLOTS:
                # Small grace window handles normal scheduler jitter and
                # startup just after the scheduled minute, not hours-late posts.
                if now.hour == hour and now.minute < _SLOT_WINDOW_MINUTES:
                    await _send_to_active_groups(bot, slot_name, day_key)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Daily ALEXA group promotion scheduler iteration failed")
        await asyncio.sleep(_CHECK_INTERVAL_SECONDS)


def start_daily_promotion_scheduler(bot) -> None:
    global _scheduler_task
    if _scheduler_task is None or _scheduler_task.done():
        _scheduler_task = asyncio.create_task(
            _daily_promotion_scheduler(bot),
            name="alexa-daily-group-promotion",
        )


async def stop_daily_promotion_scheduler() -> None:
    global _scheduler_task
    if _scheduler_task and not _scheduler_task.done():
        _scheduler_task.cancel()
        try:
            await _scheduler_task
        except asyncio.CancelledError:
            pass
    _scheduler_task = None
