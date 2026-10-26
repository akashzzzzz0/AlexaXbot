"""World events: alternating per-group Speed Type and Name Guess challenges."""
import asyncio
import html
import random
import re
import time
from datetime import datetime, timedelta, timezone

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import select, func

from bot.config import settings
from bot.database.connection import async_session_maker
from bot.database.models import (
    Group, GuessPhraseEvent, GuessNumberEvent,
    SpeedTypeEvent, SpeedTypeSubmission, SpeedTypeStreak,
)
from bot.services.economy_service import add_coins

router = Router()
EVENT_INTERVAL_SECONDS = 3 * 60 * 60
EVENT_DURATION_MINUTES = 180
SPEED_REWARDS = (500, 250, 100)
STREAK_BONUS = 200
_event_scheduler_task = None

# Every group gets a separately selected prompt; prompts are intentionally short
# enough to type accurately on mobile.
TYPE_CHALLENGES = [
    "Alexa makes every group legendary!",
    "Neon lights guide the fastest player.",
    "Speed wins when accuracy comes first.",
    "The quick fox jumps over the lazy dog.",
    "Teamwork turns small wins into legends.",
    "Cyber stars shine brightest at midnight.",
    "Practice makes every champion faster.",
    "Stay focused and type every word correctly.",
    "A little speed creates a big victory.",
    "The mystery begins when the spoiler opens.",
    "Fast fingers need a sharp mind.",
    "Every champion starts with one correct answer.",
    "Blue neon sparks light up the arena.",
    "Accuracy is more important than rushing.",
    "Alexa rewards the fastest correct typist.",
]

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
TYPE_IMAGE = "https://images.unsplash.com/photo-1516321318423-f06f85e504b3?auto=format&fit=crop&fm=jpg&q=80&w=1200"
NAME_IMAGE = "https://images.unsplash.com/photo-1519682337058-a94d519337bc?auto=format&fit=crop&fm=jpg&q=80&w=1200"


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _normalize_answer(value: str) -> str:
    """Normalize whitespace and case, while preserving punctuation and words."""
    return " ".join(value.casefold().split())


def _elapsed_seconds(started_at: datetime, now: datetime) -> float:
    return max(0.0, (_utc(now) - _utc(started_at)).total_seconds())


def _typing_event_caption(challenge: str) -> str:
    safe_challenge = html.escape(challenge)
    return (
        "⚡ <b>ALEXA SPEED TYPE CHALLENGE</b> ⚡\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🖼️ Tap the spoiler image to reveal the challenge.\n"
        f"⌨️ Type this text exactly in the group:\n<tg-spoiler>{safe_challenge}</tg-spoiler>\n\n"
        "🏆 First 3 correct entries earn rewards:\n"
        "🥇 500 · 🥈 250 · 🥉 100 coins\n"
        "🔥 3 consecutive first-place wins: +200 bonus coins\n"
        "⏱️ Your time is measured from the event post to your correct message.\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "⚠️ Exact text required — speed only counts with accuracy!"
    )


def _phrase_event_text(prize: int, prefix: str) -> str:
    return (
        "⚡ <b>ALEXA EVENT ACTIVATED</b> ⚡\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🧩 <b>SECRET NAME CHALLENGE</b>\n\n"
        f"🔎 Guess the rest of the name! Alexa reveals: <b>{html.escape(prefix)}</b> …\n"
        "💬 Your answer: <code>/n your answer</code>\n\n"
        f"💰 <b>Prize: {prize:,} Coins</b>\n"
        f"⏳ <b>Duration: {EVENT_DURATION_MINUTES // 60} hours</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🏆 First correct answer wins!\n"
        "🎁 Tap the spoiler image to reveal the mystery artwork."
    )


async def _has_active_event(session, chat_id: int, now: datetime) -> bool:
    typing_id = await session.scalar(
        select(SpeedTypeEvent.id).where(
            SpeedTypeEvent.chat_id == chat_id,
            SpeedTypeEvent.status == "active",
            SpeedTypeEvent.ends_at > now,
        ).limit(1)
    )
    if typing_id:
        return True
    phrase_id = await session.scalar(
        select(GuessPhraseEvent.id).where(
            GuessPhraseEvent.chat_id == chat_id,
            GuessPhraseEvent.status == "active",
            GuessPhraseEvent.ends_at > now,
        ).limit(1)
    )
    return phrase_id is not None


async def _next_event_type(session, chat_id: int) -> str:
    """Alternate Speed Type and Name Guess per group, starting with Speed Type."""
    typing = await session.scalar(
        select(SpeedTypeEvent).where(SpeedTypeEvent.chat_id == chat_id)
        .order_by(SpeedTypeEvent.starts_at.desc()).limit(1)
    )
    phrase = await session.scalar(
        select(GuessPhraseEvent).where(GuessPhraseEvent.chat_id == chat_id)
        .order_by(GuessPhraseEvent.starts_at.desc()).limit(1)
    )
    if typing is None and phrase is None:
        return "typing"
    if typing is None:
        return "typing"
    if phrase is None:
        return "phrase"
    return "phrase" if _utc(typing.starts_at) >= _utc(phrase.starts_at) else "typing"


async def _create_event(bot, chat_id: int, chat_title: str, created_by: int | None = None) -> bool:
    now = datetime.now(timezone.utc)
    event_type = "typing"
    prize = random.randint(500, 2000)
    low, high = 100, 500
    phrase_row = None
    typing_row = None

    async with async_session_maker() as session:
        if await _has_active_event(session, chat_id, now):
            return False
        event_type = await _next_event_type(session, chat_id)
        if event_type == "typing":
            challenge = random.choice(TYPE_CHALLENGES)
            typing_row = SpeedTypeEvent(
                chat_id=chat_id,
                chat_title=(chat_title or "Group")[:256],
                challenge_text=challenge,
                prize_first=SPEED_REWARDS[0],
                prize_second=SPEED_REWARDS[1],
                prize_third=SPEED_REWARDS[2],
                starts_at=now,
                ends_at=now + timedelta(minutes=EVENT_DURATION_MINUTES),
                status="active",
                created_by=created_by,
            )
            session.add(typing_row)
        else:
            title, prefix, answer = random.choice(PHRASES)
            phrase_row = GuessPhraseEvent(
                chat_id=chat_id, chat_title=(chat_title or "Group")[:256],
                title=title, prefix=prefix, answer=answer, prize=prize,
                starts_at=now, ends_at=now + timedelta(minutes=EVENT_DURATION_MINUTES),
                status="active", created_by=created_by,
            )
            session.add(phrase_row)
        await session.commit()
        typing_id = typing_row.id if typing_row else None
        phrase_id = phrase_row.id if phrase_row else None

    try:
        if event_type == "typing":
            await bot.send_photo(
                chat_id,
                photo=TYPE_IMAGE,
                caption=_typing_event_caption(typing_row.challenge_text),
                parse_mode="HTML",
                has_spoiler=True,
            )
        else:
            await bot.send_photo(
                chat_id,
                photo=NAME_IMAGE,
                caption=_phrase_event_text(prize, prefix),
                parse_mode="HTML",
                has_spoiler=True,
            )
    except Exception:
        async with async_session_maker() as session:
            saved = (
                await session.get(SpeedTypeEvent, typing_id)
                if event_type == "typing"
                else await session.get(GuessPhraseEvent, phrase_id)
            )
            if saved:
                saved.status = "cancelled"
                await session.commit()
        return False
    return True


async def _close_expired_events(bot) -> None:
    now = datetime.now(timezone.utc)
    async with async_session_maker() as session:
        typing_rows = (await session.scalars(select(SpeedTypeEvent).where(
            SpeedTypeEvent.status == "active", SpeedTypeEvent.ends_at <= now,
        ))).all()
        phrase_rows = (await session.scalars(select(GuessPhraseEvent).where(
            GuessPhraseEvent.status == "active", GuessPhraseEvent.ends_at <= now,
        ))).all()
        notices = [(row.chat_id, "typing") for row in typing_rows]
        notices.extend((row.chat_id, "phrase") for row in phrase_rows)
        for row in [*typing_rows, *phrase_rows]:
            row.status = "expired"
        await session.commit()
    for chat_id, kind in notices:
        try:
            if kind == "typing":
                message = "⏳ <b>Speed Type Challenge ended!</b> The event timer is over."
            else:
                message = "⏳ <b>Name Challenge ended!</b> Nobody completed the name in time."
            await bot.send_message(chat_id, message + " The next event will arrive in the cycle.", parse_mode="HTML")
        except Exception:
            pass


async def _event_scheduler(bot) -> None:
    # Post at startup, then every three hours. Event types alternate per group.
    while True:
        try:
            await _close_expired_events(bot)
            async with async_session_maker() as session:
                groups = (await session.scalars(select(Group).where(Group.is_active.is_(True)))).all()
                targets = [(group.id, group.title) for group in groups]
            for chat_id, title in targets:
                try:
                    await _create_event(bot, chat_id, title or "Group")
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


@router.message(Command(commands=["n", "name"]), F.chat.type.in_({"group", "supergroup"}))
async def guess_name(message: Message, db_session, db_user, **kwargs):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        await message.reply("🧩 Use <code>/n remaining words</code> to guess the rest of the name.", parse_mode="HTML")
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
    event.status = "won"
    event.winner_user_id = db_user.id
    event.winner_telegram_id = message.from_user.id
    event.winner_name = message.from_user.full_name[:256]
    event.winning_answer = parts[1][:256]
    await add_coins(
        db_session, db_user.id, event.prize,
        tx_type="world_event_reward",
        description=f"Guess the name event prize in chat {message.chat.id}",
        reference_id=f"name_event:{event.id}",
    )
    await db_session.commit()
    await message.reply(
        "🎉 <b>We have a winner!</b>\n\n"
        f"🏆 {message.from_user.mention_html()} completed <b>{html.escape(event.title)}</b>!\n"
        f"🪙 Prize: <b>+{event.prize:,} coins</b>",
        parse_mode="HTML",
    )


@router.message(F.chat.type.in_({"group", "supergroup"}), F.text)
async def speed_type_submission(message: Message, db_session, db_user, **kwargs):
    """Check ordinary group messages against the current spoiler typing prompt."""
    raw_text = message.text or ""
    if not raw_text or raw_text.lstrip().startswith("/"):
        return
    now = datetime.now(timezone.utc)
    event = await db_session.scalar(select(SpeedTypeEvent).where(
        SpeedTypeEvent.chat_id == message.chat.id,
        SpeedTypeEvent.status == "active",
        SpeedTypeEvent.ends_at > now,
    ).order_by(SpeedTypeEvent.starts_at.desc()).limit(1))
    if not event or _normalize_answer(raw_text) != _normalize_answer(event.challenge_text):
        return

    existing = await db_session.scalar(select(SpeedTypeSubmission.id).where(
        SpeedTypeSubmission.event_id == event.id,
        SpeedTypeSubmission.user_id == db_user.id,
    ).limit(1))
    if existing:
        return

    count = await db_session.scalar(select(func.count(SpeedTypeSubmission.id)).where(
        SpeedTypeSubmission.event_id == event.id,
    ))
    placement = int(count or 0) + 1
    if placement > 3:
        return
    elapsed_ms = int(_elapsed_seconds(event.starts_at, now) * 1000)
    reward = (event.prize_first, event.prize_second, event.prize_third)[placement - 1]
    session_streak = await db_session.scalar(select(SpeedTypeStreak).where(
        SpeedTypeStreak.chat_id == message.chat.id,
        SpeedTypeStreak.user_id == db_user.id,
    ).limit(1))
    bonus = 0
    if placement == 1:
        if session_streak is None:
            session_streak = SpeedTypeStreak(chat_id=message.chat.id, user_id=db_user.id, streak_count=0)
            db_session.add(session_streak)
        session_streak.streak_count = (session_streak.streak_count or 0) + 1
        if session_streak.streak_count % 3 == 0:
            bonus = STREAK_BONUS
    elif session_streak is not None:
        session_streak.streak_count = 0

    submission = SpeedTypeSubmission(
        event_id=event.id,
        chat_id=message.chat.id,
        user_id=db_user.id,
        telegram_id=message.from_user.id,
        winner_name=(message.from_user.full_name or message.from_user.first_name or "Player")[:256],
        elapsed_ms=elapsed_ms,
        placement=placement,
        reward=reward,
        submitted_at=now,
    )
    db_session.add(submission)
    await add_coins(
        db_session, db_user.id, reward,
        tx_type="speed_type_reward",
        description=f"Speed Type Challenge place {placement} in chat {message.chat.id}",
        reference_id=f"speed_type:{event.id}:{placement}",
    )
    if bonus:
        await add_coins(
            db_session, db_user.id, bonus,
            tx_type="speed_type_streak_bonus",
            description=f"Three consecutive first-place Speed Type wins in chat {message.chat.id}",
            reference_id=f"speed_type_streak:{event.id}",
        )
    if placement == 3:
        event.status = "completed"
    await db_session.commit()

    seconds = elapsed_ms / 1000
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    response = (
        f"{medals[placement]} <b>Speed Type result!</b>\n"
        f"👤 {message.from_user.mention_html()} got place <b>#{placement}</b>\n"
        f"⏱️ Time from event post: <b>{seconds:.2f} seconds</b>\n"
        f"🪙 Reward: <b>+{reward:,} coins</b>"
    )
    if bonus:
        response += f"\n🔥 3-win streak bonus: <b>+{bonus:,} coins</b>!"
    await message.reply(response, parse_mode="HTML")


@router.message(Command("g"), F.chat.type.in_({"group", "supergroup"}))
async def legacy_guess_number(message: Message, db_session, db_user, **kwargs):
    """Compatibility for any already-active legacy number event; new events use typing."""
    parts = (message.text or "").split(maxsplit=1)
    now = datetime.now(timezone.utc)
    event = await db_session.scalar(select(GuessNumberEvent).where(
        GuessNumberEvent.chat_id == message.chat.id,
        GuessNumberEvent.status == "active", GuessNumberEvent.ends_at > now,
    ).order_by(GuessNumberEvent.starts_at.desc()).limit(1))
    if not event:
        await message.reply("⌨️ Current Speed Type event: tap the spoiler image and type the revealed text directly in the group.")
        return
    if len(parts) != 2:
        await message.reply("🕹️ Use /g followed by a number for the older number event.")
        return
    try:
        guess = int(parts[1].strip())
    except ValueError:
        await message.reply("Please enter a whole number between 100 and 500.")
        return
    if not 100 <= guess <= 500:
        await message.reply("🎯 The number must be between 100 and 500.")
        return
    if guess != event.secret_number:
        await message.reply("❌ Not the secret number. Try again!")
        return
    event.status = "won"
    event.winner_user_id = db_user.id
    event.winner_telegram_id = message.from_user.id
    event.winner_name = message.from_user.full_name[:256]
    event.winning_guess = guess
    await add_coins(
        db_session, db_user.id, event.prize,
        tx_type="world_event_reward",
        description=f"Legacy guess number event prize in chat {message.chat.id}",
        reference_id=f"guess_event:{event.id}",
    )
    await db_session.commit()
    await message.reply(
        "🎉 <b>We have a winner!</b>\n\n"
        f"🏆 {message.from_user.mention_html()} guessed <b>{guess}</b> correctly!\n"
        f"🪙 Prize: <b>+{event.prize:,} coins</b>",
        parse_mode="HTML",
    )


@router.message(Command("eventnow"), F.chat.type.in_({"group", "supergroup"}))
async def owner_start_event(message: Message, **kwargs):
    if not settings.effective_owner_id or message.from_user.id != settings.effective_owner_id:
        await message.reply("⛔ This command is available only to the bot owner.")
        return
    created = await _create_event(
        message.bot, message.chat.id, message.chat.title or "Group",
        created_by=message.from_user.id,
    )
    if not created:
        await message.reply("🎮 This group already has an active World Event.")
