import asyncio
import random
import re
from datetime import datetime, timedelta, timezone

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.dispatcher.event.bases import SkipHandler
from sqlalchemy import select, update

from bot.config import settings
from bot.database.connection import async_session_maker
from bot.database.models import Group, SpeedTypeEvent, SpeedTypeStreak, SpeedTypeSubmission
from bot.services.economy_service import add_coins

router = Router()
EVENT_INTERVAL_SECONDS = 3 * 60 * 60
EVENT_DURATION_MINUTES = 180
FIRST_PLACE_REWARD = 250
STREAK_BONUS = 200
STREAK_TARGET = 3
_event_scheduler_task = None

# Short, varied typing prompts. A prompt is selected separately for each group/event.
CHALLENGES = [
    "Alexa makes every group more fun!",
    "Speed typing champion is here!",
    "Neon lights, fast fingers, big wins!",
    "Teamwork makes the dream work!",
    "Stay sharp and type it right!",
    "Every second counts in this challenge!",
    "Mystery unlocked, challenge accepted!",
    "Fast typing wins the Alexa event!",
    "Keep calm and type with speed!",
    "One group, one champion, one reward!",
    "Cyber stars shine brighter at night!",
    "Practice makes progress every single day!",
    "Quick fingers and a focused mind!",
    "Your next win starts with one word!",
    "Alexa brings a new challenge to the group!",
    "Never rush so much that you miss a letter!",
    "Type the exact phrase to claim victory!",
    "The fastest correct answer takes the crown!",
    "Make every character count today!",
    "A new mystery is waiting for you!",
]

# Public image URL used as the event's attached, spoiler-hidden artwork.
CHALLENGE_IMAGE = (
    "https://images.unsplash.com/photo-1516321318423-f06f85e504b3"
    "?auto=format&fit=crop&fm=jpg&q=80&w=1200"
)


def _normalize_text(value: str) -> str:
    """Ignore case and repeated/outer whitespace, but preserve punctuation and words."""
    return re.sub(r"\\s+", " ", (value or "").strip()).casefold()


def _event_caption(prize: int, challenge: str) -> str:
    return (
        "⚡ <b>ALEXA SPEED TYPE CHALLENGE</b> ⚡\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🖼️ Tap the spoiler to reveal the attached challenge image.\n"
        "⌨️ Type the exact hidden text below directly in this group.\n\n"
        f"🙈 <tg-spoiler>{challenge}</tg-spoiler>\n\n"
        f"🏆 <b>First correct entry wins {prize:,} coins!</b>\n"
        "🔒 Once someone wins, this event closes immediately.\n"
        f"🔥 Win 3 consecutive events for a +{STREAK_BONUS} coin streak bonus.\n"
        f"⏳ Duration: {EVENT_DURATION_MINUTES // 60} hours"
    )


async def _has_active_event(session, chat_id: int, now: datetime) -> bool:
    return bool(await session.scalar(
        select(SpeedTypeEvent.id).where(
            SpeedTypeEvent.chat_id == chat_id,
            SpeedTypeEvent.status == "active",
            SpeedTypeEvent.ends_at > now,
        ).limit(1)
    ))


async def _create_event(bot, chat_id: int, chat_title: str, created_by: int | None = None) -> bool:
    now = datetime.now(timezone.utc)
    async with async_session_maker() as session:
        if await _has_active_event(session, chat_id, now):
            return False
        active_challenges = set((await session.scalars(select(SpeedTypeEvent.challenge_text).where(
            SpeedTypeEvent.status == "active", SpeedTypeEvent.ends_at > now,
        ))).all())
        unused_challenges = [item for item in CHALLENGES if item not in active_challenges]
        challenge = random.choice(unused_challenges or CHALLENGES)
        event = SpeedTypeEvent(
            chat_id=chat_id,
            chat_title=(chat_title or "Group")[:256],
            challenge_text=challenge,
            prize_first=FIRST_PLACE_REWARD,
            prize_second=0,
            prize_third=0,
            starts_at=now,
            ends_at=now + timedelta(minutes=EVENT_DURATION_MINUTES),
            status="active",
            created_by=created_by,
        )
        session.add(event)
        await session.commit()
        event_id = event.id

    try:
        await bot.send_photo(
            chat_id=chat_id,
            photo=CHALLENGE_IMAGE,
            caption=_event_caption(FIRST_PLACE_REWARD, challenge),
            parse_mode="HTML",
            has_spoiler=True,
        )
    except Exception:
        async with async_session_maker() as session:
            saved = await session.get(SpeedTypeEvent, event_id)
            if saved and saved.status == "active":
                saved.status = "cancelled"
                await session.commit()
        return False
    return True


async def _close_expired_events(bot) -> None:
    now = datetime.now(timezone.utc)
    async with async_session_maker() as session:
        expired = (await session.scalars(select(SpeedTypeEvent).where(
            SpeedTypeEvent.status == "active",
            SpeedTypeEvent.ends_at <= now,
        ))).all()
        notices = [(row.chat_id, row.challenge_text) for row in expired]
        for row in expired:
            row.status = "expired"
        await session.commit()
    for chat_id, _challenge in notices:
        try:
            await bot.send_message(
                chat_id,
                "⏳ <b>Alexa Speed Type Challenge ended!</b> No one typed the exact text in time. "
                "The next challenge arrives in the next event cycle.",
                parse_mode="HTML",
            )
        except Exception:
            pass


async def _event_scheduler(bot) -> None:
    # Create an event at startup and then every three hours for each active group.
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
        _event_scheduler_task = asyncio.create_task(_event_scheduler(bot), name="alexa-speed-type-events")


async def stop_event_scheduler() -> None:
    global _event_scheduler_task
    if _event_scheduler_task and not _event_scheduler_task.done():
        _event_scheduler_task.cancel()
        try:
            await _event_scheduler_task
        except asyncio.CancelledError:
            pass
    _event_scheduler_task = None


@router.message(
    F.text,
    F.chat.type.in_({"group", "supergroup"}),
    ~F.text.startswith("/"),
)
async def submit_speed_type(message: Message, db_session, db_user, **kwargs):
    if not message.from_user or message.from_user.is_bot or db_user is None:
        return

    now = datetime.now(timezone.utc)
    event = await db_session.scalar(select(SpeedTypeEvent).where(
        SpeedTypeEvent.chat_id == message.chat.id,
        SpeedTypeEvent.status == "active",
        SpeedTypeEvent.ends_at > now,
    ).order_by(SpeedTypeEvent.starts_at.desc()).limit(1))
    if not event:
        raise SkipHandler

    if _normalize_text(message.text or "") != _normalize_text(event.challenge_text):
        raise SkipHandler

    # Atomically claim the event so only one member can win, even if two correct
    # messages arrive nearly simultaneously.
    result = await db_session.execute(
        update(SpeedTypeEvent)
        .where(SpeedTypeEvent.id == event.id, SpeedTypeEvent.status == "active")
        .values(status="won")
    )
    if result.rowcount != 1:
        await db_session.rollback()
        return

    elapsed_ms = max(0, int((now - event.starts_at).total_seconds() * 1000))
    elapsed_seconds = elapsed_ms / 1000.0

    previous_winner = await db_session.scalar(
        select(SpeedTypeSubmission.user_id)
        .where(SpeedTypeSubmission.chat_id == message.chat.id)
        .order_by(SpeedTypeSubmission.submitted_at.desc())
        .limit(1)
    )
    streak = await db_session.scalar(select(SpeedTypeStreak).where(
        SpeedTypeStreak.chat_id == message.chat.id,
        SpeedTypeStreak.user_id == db_user.id,
    ))
    if previous_winner == db_user.id and streak is not None:
        streak.streak_count = (streak.streak_count or 0) + 1
    else:
        # A different member won the previous event, so this is a fresh streak.
        await db_session.execute(
            update(SpeedTypeStreak)
            .where(SpeedTypeStreak.chat_id == message.chat.id)
            .values(streak_count=0)
        )
        if streak is None:
            streak = SpeedTypeStreak(
                chat_id=message.chat.id, user_id=db_user.id, streak_count=1
            )
            db_session.add(streak)
        else:
            streak.streak_count = 1

    db_session.add(SpeedTypeSubmission(
        event_id=event.id,
        chat_id=message.chat.id,
        user_id=db_user.id,
        telegram_id=message.from_user.id,
        winner_name=message.from_user.full_name[:256],
        elapsed_ms=elapsed_ms,
        placement=1,
        reward=FIRST_PLACE_REWARD,
        submitted_at=now,
    ))
    bonus = STREAK_BONUS if streak.streak_count == STREAK_TARGET else 0
    total_reward = FIRST_PLACE_REWARD + bonus
    await add_coins(
        db_session,
        db_user.id,
        FIRST_PLACE_REWARD,
        tx_type="world_event_reward",
        description=f"Alexa Speed Type Challenge first-place prize in chat {message.chat.id}",
        reference_id=f"speed_type_event:{event.id}",
    )
    if bonus:
        await add_coins(
            db_session,
            db_user.id,
            bonus,
            tx_type="world_event_streak_bonus",
            description=f"Three consecutive Alexa Speed Type Challenge wins in chat {message.chat.id}",
            reference_id=f"speed_type_streak:{message.chat.id}:{db_user.id}:{streak.streak_count}",
        )
    await db_session.commit()

    streak_note = (
        f"\n🔥 <b>3-win streak bonus: +{bonus} coins!</b>"
        if bonus else
        f"\n🔥 Current consecutive wins: <b>{streak.streak_count}</b>/3"
    )
    await message.reply(
        "🎉 <b>ALEXA SPEED TYPE CHAMPION!</b>\n\n"
        f"🏆 {message.from_user.mention_html()} typed the exact text first!\n"
        f"⏱️ Completion time: <b>{elapsed_seconds:.2f} seconds</b> from event launch.\n"
        f"🪙 Reward: <b>+{FIRST_PLACE_REWARD} coins</b>"
        + (f" + <b>{bonus} bonus coins</b>" if bonus else "")
        + f"\n💰 Total earned: <b>+{total_reward} coins</b>"
        + streak_note
        + "\n🔒 This event is now closed. Only one winner per event.",
        parse_mode="HTML",
    )


@router.message(Command("eventnow"), F.chat.type.in_({"group", "supergroup"}))
async def owner_start_event(message: Message, **kwargs):
    if not message.from_user or not settings.effective_owner_id or message.from_user.id != settings.effective_owner_id:
        await message.reply("⛔ This command is available only to the bot owner.")
        return
    created = await _create_event(
        message.bot,
        message.chat.id,
        message.chat.title or "Group",
        created_by=message.from_user.id,
    )
    if not created:
        await message.reply("🎮 This group already has an active Speed Type Challenge.")
