import asyncio
import io
import random
import re
from datetime import datetime, timedelta, timezone

from PIL import Image, ImageDraw, ImageFont

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message
from aiogram.dispatcher.event.bases import SkipHandler
from sqlalchemy import select, update

from bot.config import settings
from bot.database.connection import async_session_maker
from bot.database.models import Group, SpeedTypeEvent, SpeedTypeStreak, SpeedTypeSubmission
from bot.services.economy_service import add_coins

router = Router()
EVENT_INTERVAL_SECONDS = 3 * 60 * 60
EVENT_DURATION_MINUTES = 180
WINNER_REWARD = 50
_event_scheduler_task = None

CHALLENGES = [
    "Alexa makes every group more fun",
    "Speed typing champion is here",
    "Neon lights fast fingers big wins",
    "Teamwork makes the dream work",
    "Stay sharp and type it right",
    "Every second counts in this challenge",
    "Mystery unlocked challenge accepted",
    "Fast typing wins the Alexa event",
    "Keep calm and type with speed",
    "One group one champion one reward",
    "Cyber stars shine brighter at night",
    "Practice makes progress every single day",
    "Quick fingers and a focused mind",
    "Your next win starts with one word",
    "Alexa brings a new challenge to the group",
    "Never rush so much that you miss a letter",
    "Type the exact phrase to claim victory",
    "The fastest correct answer takes the crown",
    "Make every character count today",
    "A new mystery is waiting for you",
]


def _make_spoiler_image(challenge: str) -> BufferedInputFile:
    """Draw the prompt into the actual picture; Telegram hides the complete image as a spoiler."""
    width, height = 1200, 800
    image = Image.new("RGB", (width, height), (7, 8, 25))
    draw = ImageDraw.Draw(image)
    for y in range(height):
        shade = int(16 * y / height)
        draw.line((0, y, width, y), fill=(7 + shade // 3, 8 + shade // 4, 25 + shade * 2))
    draw.rounded_rectangle((35, 35, width - 35, height - 35), radius=34, outline=(0, 240, 255), width=5)
    draw.rounded_rectangle((48, 48, width - 48, height - 48), radius=28, outline=(190, 40, 255), width=2)
    for x in range(90, width - 90, 100):
        draw.line((x, 90, x + 24, 90), fill=(0, 125, 190), width=3)
        draw.line((x, height - 90, x + 24, height - 90), fill=(120, 35, 190), width=3)
    try:
        title_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 44)
        prompt_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 52)
        hint_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 27)
    except OSError:
        title_font = prompt_font = hint_font = ImageFont.load_default()
    title = "ALEXA  SPEED TYPE"
    title_box = draw.textbbox((0, 0), title, font=title_font)
    draw.text(((width - (title_box[2] - title_box[0])) / 2, 150), title, font=title_font, fill=(0, 245, 255), stroke_width=1, stroke_fill=(170, 35, 255))
    draw.text((width / 2, 245), "HIDDEN CHALLENGE", font=hint_font, fill=(210, 180, 255), anchor="mm")
    lines, line = [], ""
    for word in challenge.split():
        candidate = f"{line} {word}".strip()
        if draw.textbbox((0, 0), candidate, font=prompt_font)[2] > width - 190 and line:
            lines.append(line)
            line = word
        else:
            line = candidate
    if line:
        lines.append(line)
    line_height = 75
    start_y = 400 - (len(lines) * line_height) // 2
    for i, text_line in enumerate(lines):
        draw.text((width / 2, start_y + i * line_height), text_line, font=prompt_font, fill=(255, 255, 255), anchor="mm", stroke_width=2, stroke_fill=(0, 160, 255))
    draw.text((width / 2, 635), "TAP TO REVEAL - TYPE IT EXACTLY", font=hint_font, fill=(255, 70, 220), anchor="mm")
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return BufferedInputFile(output.getvalue(), filename="alexa_speed_type_spoiler.png")


def _event_caption() -> str:
    return (
        "⚡ <b>ALEXA SPEED TYPE CHALLENGE</b> ⚡\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🖼️ Tap the spoiler image to reveal the text written inside it.\n"
        "⌨️ Type that exact text in this group. No command needed.\n"
        f"🏆 First member with the correct text wins <b>{WINNER_REWARD} coins</b>!\n"
        "⏱️ Alexa will announce the winner and their typing time.\n"
        f"⏳ Challenge duration: {EVENT_DURATION_MINUTES // 60} hours"
    )



def _normalize_text(value: str) -> str:
    """Ignore case and repeated/outer whitespace, but preserve punctuation and words."""
    return re.sub(r"\s+", " ", (value or "").strip()).casefold()


def _as_utc(value: datetime) -> datetime:
    """Treat naive database datetimes as UTC and normalize aware values to UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _event_caption(challenge: str) -> str:
    return (
        "⚡ <b>ALEXA SPEED TYPE CHALLENGE</b> ⚡\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🖼️ Tap the spoiler to reveal the attached challenge image.\n"
        "⌨️ Type the exact hidden text below directly in this group.\n\n"
        f"🙈 <tg-spoiler>{challenge}</tg-spoiler>\n\n"
        f"🏆 <b>Top 3 correct entries win:</b>\n🥇 {FIRST_PLACE_REWARD} · 🥈 {SECOND_PLACE_REWARD} · 🥉 {THIRD_PLACE_REWARD} coins\n"
        "🔒 The challenge closes after 3 correct entries.\n"
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
            prize_first=WINNER_REWARD,
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
            photo=_make_spoiler_image(challenge),
            caption=_event_caption(),
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
    # Ordinary members and admins can win; no command or special permission is needed.
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

    elapsed_ms = max(0, int((now - _as_utc(event.starts_at)).total_seconds() * 1000))
    elapsed_seconds = elapsed_ms / 1000.0
    # Conditional claim ensures only the first correct entry receives the reward.
    claim = await db_session.execute(
        update(SpeedTypeEvent)
        .where(
            SpeedTypeEvent.id == event.id,
            SpeedTypeEvent.status == "active",
            SpeedTypeEvent.ends_at > now,
        )
        .values(status="completed")
    )
    if claim.rowcount != 1:
        await db_session.rollback()
        raise SkipHandler

    db_session.add(SpeedTypeSubmission(
        event_id=event.id,
        chat_id=message.chat.id,
        user_id=db_user.id,
        telegram_id=message.from_user.id,
        winner_name=message.from_user.full_name[:256],
        elapsed_ms=elapsed_ms,
        placement=1,
        reward=WINNER_REWARD,
        submitted_at=now,
    ))
    await add_coins(
        db_session,
        db_user.id,
        WINNER_REWARD,
        tx_type="world_event_reward",
        description=f"Alexa Speed Type Challenge winner in chat {message.chat.id}",
        reference_id=f"speed_type_event:{event.id}:winner",
    )
    await db_session.commit()
    await message.reply(
        "🏆 <b>ALEXA SPEED TYPE WINNER!</b> 🏆\n"
        f"👑 {message.from_user.mention_html()} typed the challenge correctly first!\n"
        f"⚡ Typing time: <b>{elapsed_seconds:.2f} seconds</b>\n"
        f"🪙 Reward: <b>+{WINNER_REWARD} coins</b>\n"
        "🎉 The challenge is now closed. Next event arrives in the next 3-hour cycle!",
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
