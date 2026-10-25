import asyncio
import random
from datetime import datetime, timedelta, timezone

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select, update

from bot.config import settings
from bot.database.connection import async_session_maker
from bot.database.models import Group, GuessNumberEvent
from bot.services.economy_service import add_coins

router = Router()
EVENT_INTERVAL_SECONDS = 6 * 60 * 60
EVENT_DURATION_MINUTES = 60
_event_scheduler_task = None


def _event_text(prize: int, low: int, high: int) -> str:
    return (
        "📦 <b>New event dropped: Guess the secret number to win coins!</b>\n\n"
        f"💰 Prize: <b>{prize:,} coins</b>\n"
        f"🎯 Range: [ <b>{low} ──── {high}</b> ]\n"
        "🕹️ Guess using: <code>/g &lt;number&gt;</code>\n"
        f"⏳ Time limit: <b>{EVENT_DURATION_MINUTES} minutes</b>\n\n"
        "🏆 First person to guess the right number wins!"
    )


async def _create_event(bot, chat_id: int, chat_title: str, created_by: int | None = None) -> bool:
    low, high = 100, 500
    now = datetime.now(timezone.utc)
    async with async_session_maker() as session:
        active = await session.scalar(
            select(GuessNumberEvent).where(
                GuessNumberEvent.chat_id == chat_id,
                GuessNumberEvent.status == "active",
                GuessNumberEvent.ends_at > now,
            )
        )
        if active:
            return False
        prize = random.randint(500, 2000)
        session.add(GuessNumberEvent(
            chat_id=chat_id,
            chat_title=chat_title[:256],
            secret_number=random.randint(low, high),
            prize=prize,
            starts_at=now,
            ends_at=now + timedelta(minutes=EVENT_DURATION_MINUTES),
            status="active",
            created_by=created_by,
        ))
        await session.commit()
    try:
        await bot.send_message(chat_id, _event_text(prize, low, high))
    except Exception:
        async with async_session_maker() as session:
            saved = await session.scalar(
                select(GuessNumberEvent).where(
                    GuessNumberEvent.chat_id == chat_id,
                    GuessNumberEvent.status == "active",
                    GuessNumberEvent.starts_at == now,
                )
            )
            if saved:
                saved.status = "cancelled"
                await session.commit()
        return False
    return True


async def _close_expired_events(bot) -> None:
    now = datetime.now(timezone.utc)
    async with async_session_maker() as session:
        rows = (await session.scalars(
            select(GuessNumberEvent).where(
                GuessNumberEvent.status == "active",
                GuessNumberEvent.ends_at <= now,
            )
        )).all()
        notices = [row.chat_id for row in rows]
        for row in rows:
            row.status = "expired"
        await session.commit()
    for chat_id in notices:
        try:
            await bot.send_message(
                chat_id,
                "⏳ <b>Guess event ended!</b> Nobody guessed the secret number in time. The next event drops automatically.",
            )
        except Exception:
            pass


async def _event_scheduler(bot) -> None:
    # First event is dropped on startup; subsequent drops are six hours apart.
    while True:
        try:
            await _close_expired_events(bot)
            async with async_session_maker() as session:
                groups = (await session.scalars(
                    select(Group).where(Group.is_active.is_(True))
                )).all()
                targets = [(group.id, group.title) for group in groups]
            for chat_id, title in targets:
                try:
                    await _create_event(bot, chat_id, title)
                except Exception:
                    # A single group's failure must not stop other groups' events.
                    continue
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        await asyncio.sleep(EVENT_INTERVAL_SECONDS)


def start_event_scheduler(bot) -> None:
    global _event_scheduler_task
    if _event_scheduler_task is None or _event_scheduler_task.done():
        _event_scheduler_task = asyncio.create_task(_event_scheduler(bot), name="alexa-world-events")


async def stop_event_scheduler() -> None:
    global _event_scheduler_task
    if _event_scheduler_task and not _event_scheduler_task.done():
        _event_scheduler_task.cancel()
        try:
            await _event_scheduler_task
        except asyncio.CancelledError:
            pass
    _event_scheduler_task = None


@router.message(Command("g"), F.chat.type.in_({"group", "supergroup"}))
async def guess_number(message: Message, db_session, db_user, **kwargs):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        await message.reply("🕹️ Use <code>/g 250</code> to guess the number.")
        return
    try:
        guess = int(parts[1].strip())
    except ValueError:
        await message.reply("Please enter a whole number between 100 and 500.")
        return
    if not 100 <= guess <= 500:
        await message.reply("🎯 The number must be between 100 and 500.")
        return

    now = datetime.now(timezone.utc)
    event = await db_session.scalar(
        select(GuessNumberEvent).where(
            GuessNumberEvent.chat_id == message.chat.id,
            GuessNumberEvent.status == "active",
            GuessNumberEvent.ends_at > now,
        ).order_by(GuessNumberEvent.starts_at.desc())
    )
    if not event:
        await message.reply("🎮 There is no active guessing event right now. Watch for the next event drop!")
        return
    if guess != event.secret_number:
        await message.reply("❌ Not the secret number. Try again!")
        return

    # Atomic status change ensures only the first correct guess can win.
    result = await db_session.execute(
        update(GuessNumberEvent)
        .where(GuessNumberEvent.id == event.id, GuessNumberEvent.status == "active")
        .values(
            status="won",
            winner_user_id=db_user.id,
            winner_telegram_id=message.from_user.id,
            winner_name=message.from_user.full_name[:256],
            winning_guess=guess,
        )
    )
    if result.rowcount != 1:
        await db_session.rollback()
        await message.reply("🏁 This event has already been won.")
        return

    await add_coins(
        db_session,
        db_user.id,
        event.prize,
        tx_type="world_event_reward",
        description=f"Guess the number event prize in chat {message.chat.id}",
        reference_id=f"guess_event:{event.id}",
    )
    await db_session.commit()
    await message.reply(
        "🎉 <b>We have a winner!</b>\n\n"
        f"🏆 {message.from_user.mention_html()} guessed <b>{guess}</b> correctly!\n"
        f"🪙 Prize: <b>+{event.prize:,} coins</b>\n"
        "📦 The next event will drop automatically."
    )


@router.message(Command("eventnow"), F.chat.type.in_({"group", "supergroup"}))
async def owner_start_event(message: Message, **kwargs):
    if not settings.effective_owner_id or message.from_user.id != settings.effective_owner_id:
        await message.reply("⛔ This command is available only to the bot owner.")
        return
    created = await _create_event(
        message.bot,
        message.chat.id,
        message.chat.title or "Group",
        created_by=message.from_user.id,
    )
    if not created:
        await message.reply("🎮 This group already has an active guessing event.")
