import asyncio
import random
import re
from datetime import datetime, timedelta, timezone

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select, update

from bot.config import settings
from bot.database.connection import async_session_maker
from bot.database.models import Group, GuessNumberEvent, GuessPhraseEvent
from bot.services.economy_service import add_coins

router = Router()
EVENT_INTERVAL_SECONDS = 3 * 60 * 60
EVENT_DURATION_MINUTES = 180
_event_scheduler_task = None

# Each entry contains a recognizable title/name; the first three words are shown.
PHRASES = [
    ("Harry Potter and the Philosopher's Stone", "Harry Potter and", "the Philosopher's Stone"),
    ("The Lord of the Rings", "The Lord of", "the Rings"),
    ("The Legend of Zelda", "The Legend of", "Zelda"),
    ("The Fast and the Furious", "The Fast and", "the Furious"),
    ("Pirates of the Caribbean", "Pirates of the", "Caribbean"),
    ("Beauty and the Beast", "Beauty and the", "Beast"),
    ("The Wizard of Oz", "The Wizard of", "Oz"),
    ("Charlie and the Chocolate Factory", "Charlie and the", "Chocolate Factory"),
    ("The Chronicles of Narnia", "The Chronicles of", "Narnia"),
    ("Guardians of the Galaxy", "Guardians of the", "Galaxy"),
]


def _event_text(prize: int, low: int, high: int) -> str:
    return (
        "⚡ <b>ALEXA EVENT ACTIVATED</b> ⚡\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🎯 <b>SECRET CODE CHALLENGE</b>\n\n"
        "📦 A new event just dropped!\n"
        "🔢 Guess the secret 3-digit number to win.\n\n"
        f"💰 <b>Prize: {prize:,} Coins</b>\n"
        f"🎯 <b>Range: {low}–{high}</b>\n"
        "🕹️ Your guess: <code>/g 250</code>\n"
        f"⏳ <b>Duration: {EVENT_DURATION_MINUTES // 60} hours</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🏆 <b>First correct guess wins!</b>\n"
        "🎁 <b>VIP REWARD — SPOILER</b>\n"
        "✨ The mystery awaits you!"
    )


def _phrase_event_text(prize: int, prefix: str) -> str:
    return (
        "⚡ <b>ALEXA EVENT ACTIVATED</b> ⚡\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🧩 <b>SECRET NAME CHALLENGE</b>\n\n"
        f"🔎 Guess the rest of the name! Alexa reveals: <b>{prefix}</b> …\n"
        "💬 Your answer: <code>/n your answer</code>\n\n"
        f"💰 <b>Prize: {prize:,} Coins</b>\n"
        f"⏳ <b>Duration: {EVENT_DURATION_MINUTES // 60} hours</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🏆 <b>First correct answer wins!</b>\n"
        "🎁 <b>VIP REWARD — SPOILER</b>\n"
        "✨ The mystery awaits you!"
    )


async def _has_active_event(session, chat_id: int, now: datetime) -> bool:
    number_event = await session.scalar(
        select(GuessNumberEvent.id).where(
            GuessNumberEvent.chat_id == chat_id,
            GuessNumberEvent.status == "active",
            GuessNumberEvent.ends_at > now,
        ).limit(1)
    )
    if number_event:
        return True
    phrase_event = await session.scalar(
        select(GuessPhraseEvent.id).where(
            GuessPhraseEvent.chat_id == chat_id,
            GuessPhraseEvent.status == "active",
            GuessPhraseEvent.ends_at > now,
        ).limit(1)
    )
    return phrase_event is not None


async def _create_event(bot, chat_id: int, chat_title: str, created_by: int | None = None) -> bool:
    now = datetime.now(timezone.utc)
    prize = random.randint(500, 2000)
    event_type = random.choice(("number", "phrase"))
    low, high = 100, 500
    phrase_row = None

    async with async_session_maker() as session:
        if await _has_active_event(session, chat_id, now):
            return False
        if event_type == "number":
            session.add(GuessNumberEvent(
                chat_id=chat_id, chat_title=chat_title[:256],
                secret_number=random.randint(low, high), prize=prize,
                starts_at=now, ends_at=now + timedelta(minutes=EVENT_DURATION_MINUTES),
                status="active", created_by=created_by,
            ))
        else:
            title, prefix, answer = random.choice(PHRASES)
            phrase_row = GuessPhraseEvent(
                chat_id=chat_id, chat_title=chat_title[:256],
                title=title, prefix=prefix, answer=answer, prize=prize,
                starts_at=now, ends_at=now + timedelta(minutes=EVENT_DURATION_MINUTES),
                status="active", created_by=created_by,
            )
            session.add(phrase_row)
        await session.commit()
        phrase_id = phrase_row.id if phrase_row else None

    try:
        if event_type == "number":
            await bot.send_photo(
                chat_id,
                photo="https://images.unsplash.com/photo-1762279388952-85187155e48d?auto=format&fit=crop&fm=jpg&q=80&w=1200",
                caption=_event_text(prize, low, high),
                parse_mode="HTML",
                has_spoiler=True,
            )
        else:
            await bot.send_photo(
                chat_id,
                photo="https://images.unsplash.com/photo-1762279388952-85187155e48d?auto=format&fit=crop&fm=jpg&q=80&w=1200",
                caption=_phrase_event_text(prize, prefix),
                parse_mode="HTML",
                has_spoiler=True,
            )
    except Exception:
        async with async_session_maker() as session:
            if event_type == "number":
                saved = await session.scalar(select(GuessNumberEvent).where(
                    GuessNumberEvent.chat_id == chat_id,
                    GuessNumberEvent.status == "active",
                    GuessNumberEvent.starts_at == now,
                ))
            else:
                saved = await session.get(GuessPhraseEvent, phrase_id)
            if saved:
                saved.status = "cancelled"
                await session.commit()
        return False
    return True


async def _close_expired_events(bot) -> None:
    now = datetime.now(timezone.utc)
    async with async_session_maker() as session:
        number_rows = (await session.scalars(select(GuessNumberEvent).where(
            GuessNumberEvent.status == "active", GuessNumberEvent.ends_at <= now,
        ))).all()
        phrase_rows = (await session.scalars(select(GuessPhraseEvent).where(
            GuessPhraseEvent.status == "active", GuessPhraseEvent.ends_at <= now,
        ))).all()
        notices = [(row.chat_id, "number") for row in number_rows]
        notices.extend((row.chat_id, "phrase") for row in phrase_rows)
        for row in [*number_rows, *phrase_rows]:
            row.status = "expired"
        await session.commit()
    for chat_id, kind in notices:
        try:
            message = (
                "⏳ <b>Guess event ended!</b> Nobody guessed the number in time."
                if kind == "number" else
                "⏳ <b>Name event ended!</b> Nobody guessed the remaining words in time."
            )
            await bot.send_message(chat_id, message + " The next event arrives in the event cycle.")
        except Exception:
            pass


async def _event_scheduler(bot) -> None:
    # Drop an event at startup, then every three hours.
    while True:
        try:
            await _close_expired_events(bot)
            async with async_session_maker() as session:
                groups = (await session.scalars(select(Group).where(Group.is_active.is_(True)))).all()
                targets = [(group.id, group.title) for group in groups]
            for chat_id, title in targets:
                try:
                    await _create_event(bot, chat_id, title)
                except Exception:
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
    event = await db_session.scalar(select(GuessNumberEvent).where(
        GuessNumberEvent.chat_id == message.chat.id,
        GuessNumberEvent.status == "active", GuessNumberEvent.ends_at > now,
    ).order_by(GuessNumberEvent.starts_at.desc()))
    if not event:
        await message.reply("🎮 There is no active number event right now.")
        return
    if guess != event.secret_number:
        await message.reply("❌ Not the secret number. Try again!")
        return
    result = await db_session.execute(update(GuessNumberEvent)
        .where(GuessNumberEvent.id == event.id, GuessNumberEvent.status == "active")
        .values(status="won", winner_user_id=db_user.id,
                winner_telegram_id=message.from_user.id,
                winner_name=message.from_user.full_name[:256], winning_guess=guess))
    if result.rowcount != 1:
        await db_session.rollback()
        await message.reply("🏁 This event has already been won.")
        return
    await add_coins(db_session, db_user.id, event.prize,
        tx_type="world_event_reward",
        description=f"Guess the number event prize in chat {message.chat.id}",
        reference_id=f"guess_event:{event.id}")
    await db_session.commit()
    await message.reply(
        "🎉 <b>We have a winner!</b>\n\n"
        f"🏆 {message.from_user.mention_html()} guessed <b>{guess}</b> correctly!\n"
        f"🪙 Prize: <b>+{event.prize:,} coins</b>"
    )


def _normalize_answer(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9']+", value.casefold()))


@router.message(Command(commands=["n", "name"]), F.chat.type.in_({"group", "supergroup"}))
async def guess_name(message: Message, db_session, db_user, **kwargs):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        await message.reply("🧩 Use <code>/n remaining words</code> to guess the rest of the name.")
        return
    now = datetime.now(timezone.utc)
    event = await db_session.scalar(select(GuessPhraseEvent).where(
        GuessPhraseEvent.chat_id == message.chat.id,
        GuessPhraseEvent.status == "active", GuessPhraseEvent.ends_at > now,
    ).order_by(GuessPhraseEvent.starts_at.desc()))
    if not event:
        await message.reply("🎮 There is no active name event right now.")
        return
    if _normalize_answer(parts[1]) != _normalize_answer(event.answer):
        await message.reply("❌ Not quite! Guess the remaining words.")
        return
    result = await db_session.execute(update(GuessPhraseEvent)
        .where(GuessPhraseEvent.id == event.id, GuessPhraseEvent.status == "active")
        .values(status="won", winner_user_id=db_user.id,
                winner_telegram_id=message.from_user.id,
                winner_name=message.from_user.full_name[:256], winning_answer=parts[1][:256]))
    if result.rowcount != 1:
        await db_session.rollback()
        await message.reply("🏁 This event has already been won.")
        return
    await add_coins(db_session, db_user.id, event.prize,
        tx_type="world_event_reward",
        description=f"Guess the name event prize in chat {message.chat.id}",
        reference_id=f"name_event:{event.id}")
    await db_session.commit()
    await message.reply(
        "🎉 <b>We have a winner!</b>\n\n"
        f"🏆 {message.from_user.mention_html()} completed <b>{event.title}</b>!\n"
        f"🪙 Prize: <b>+{event.prize:,} coins</b>"
    )


@router.message(Command("eventnow"), F.chat.type.in_({"group", "supergroup"}))
async def owner_start_event(message: Message, **kwargs):
    if not settings.effective_owner_id or message.from_user.id != settings.effective_owner_id:
        await message.reply("⛔ This command is available only to the bot owner.")
        return
    created = await _create_event(message.bot, message.chat.id, message.chat.title or "Group",
                                  created_by=message.from_user.id)
    if not created:
        await message.reply("🎮 This group already has an active World Event.")
