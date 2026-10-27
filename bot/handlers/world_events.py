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
from bot.database.models import Group, SpeedTypeEvent, SpeedTypeSubmission
from bot.services.economy_service import add_coins

router = Router()
EVENT_INTERVAL_SECONDS = 3 * 60 * 60
EVENT_DURATION_MINUTES = 180
WINNER_REWARD = 50
_event_scheduler_task = None

CHALLENGES = [
    "CHAMPION",
    "CROPPED",
    "DROID",
    "TURBO",
    "KIT",
    "DRAG",
    "PHANTOM",
    "NOVA",
    "VORTEX",
    "PIXEL",
    "FALCON",
    "ROCKET",
    "NEBULA",
    "COMET",
    "TITAN",
    "BLAZE",
    "FROST",
    "SHADOW",
    "SPARK",
    "ECLIPSE",
    "RANGER",
    "MYSTIC",
    "COSMIC",
    "PRISM",
    "ORBIT",
    "LASER",
    "THUNDER",
    "STORM",
    "FLUX",
    "QUANTUM",
    "CYBER",
    "NINJA",
    "PIRATE",
    "WIZARD",
    "DRAGON",
    "PANTHER",
    "COBRA",
    "VIPER",
    "WOLF",
    "TIGER",
    "JAGUAR",
    "HAWK",
    "EAGLE",
    "RAVEN",
    "PHOENIX",
    "HYDRA",
    "TITANIC",
    "GLITCH",
    "BYTE",
    "CACHE",
    "CODER",
    "HACKER",
    "ROGUE",
    "SPECTRE",
    "GHOST",
    "REAPER",
    "BISHOP",
    "KNIGHT",
    "QUEEN",
    "KING",
    "ACE",
    "JOKER",
    "ROYAL",
    "LEGEND",
    "MYTHIC",
    "ELITE",
    "ULTRA",
    "OMEGA",
    "ALPHA",
    "SIGMA",
    "DELTA",
    "GAMMA",
    "ZETA",
    "KAPPA",
    "ION",
    "ATOM",
    "PLASMA",
    "RADAR",
    "SONIC",
    "NITRO",
    "BOOST",
    "SHIFT",
    "CLUTCH",
    "GEAR",
    "ENGINE",
    "PISTON",
    "AXLE",
    "WHEEL",
    "DRIFT",
    "RALLY",
    "RACER",
    "SPRINT",
    "DASH",
    "BLITZ",
    "STRIKE",
    "SLASH",
    "SMASH",
    "CRUSH",
    "BRAWL",
    "DUEL",
    "ARENA",
    "QUEST",
    "LOOT",
    "TREASURE",
    "RELIC",
    "VAULT",
    "FORTUNE",
    "JACKPOT",
    "TOKEN",
    "COIN",
    "STAR",
    "CROWN",
    "BADGE",
    "TROPHY",
    "MEDAL",
    "PRIZE",
    "BONUS",
    "REWARD",
    "MERCURY",
    "VENUS",
    "MARS",
    "JUPITER",
    "SATURN",
    "URANUS",
    "NEPTUNE",
    "PLUTO",
    "GALAXY",
    "SOLAR",
    "LUNAR",
    "STELLAR",
    "ASTRAL",
    "METEOR",
    "ASTEROID",
    "PULSAR",
    "QUASAR",
    "ZENITH",
    "NEXUS",
    "MATRIX",
    "VECTOR",
    "CIPHER",
    "ENIGMA",
    "RIDDLE",
    "PUZZLE",
    "SECRET",
    "MIRAGE",
    "ILLUSION",
    "PARADOX",
    "RIFT",
    "PORTAL",
    "WARP",
    "JUMP",
    "HYPER",
    "MEGA",
    "GIGA",
    "TERA",
    "INFERNO",
    "EMBER",
    "FLAME",
    "ASHEN",
    "BOLT",
    "VOLT",
    "WATT",
    "AMPERE",
    "MAGNET",
    "FUSION",
    "REACTOR",
    "CORE",
    "NANO",
    "MICRO",
    "MACRO",
    "FABLE",
    "FABLES",
    "SAGA",
    "EPIC",
    "QUESTOR",
    "HERO",
    "VILLAIN",
    "SENTINEL",
    "GUARDIAN",
    "AVATAR",
    "MONARCH",
    "EMPEROR",
    "EMPIRE",
    "KINGDOM",
    "CITADEL",
    "BASTION",
    "FORTRESS",
    "OUTPOST",
    "FRONTIER",
    "SUMMIT",
    "EVEREST",
    "CANYON",
    "GLACIER",
    "DESERT",
    "OASIS",
    "JUNGLE",
    "TUNDRA",
    "TEMPEST",
    "CYCLONE",
    "TORNADO",
    "TSUNAMI",
    "QUAKE",
    "AVALANCHE",
    "MAMMOTH",
    "RAPTOR",
    "RAPID",
    "SWIFT",
    "FIERCE",
    "BRISK",
    "BOLD",
    "VIVID",
    "VIVACE",
    "ZEST",
    "ZEN",
    "KARMA",
    "AURORA",
    "OPAL",
    "ONYX",
    "JADE",
    "RUBY",
    "AMBER",
    "IVORY",
    "SAPPHIRE",
    "DIAMOND",
    "CRYSTAL",
    "QUARTZ",
    "MARBLE",
    "STEEL",
    "TITANIUM",
    "CARBON",
    "COBALT",
    "CHROME",
    "NICKEL",
    "SILVER",
    "GOLDEN",
    "BRONZE",
    "COPPER",
    "IRON",
    "MOLTEN",
    "FORGE",
    "ANVIL",
    "HAMMER",
    "DAGGER",
    "SABER",
    "LANCE",
    "ARROW",
    "QUIVER",
    "SHIELD",
    "ARMOR",
    "HELMET",
    "GAUNTLET",
    "SCEPTER",
    "TRIDENT",
    "CANNON",
    "TORPEDO",
    "SUBZERO",
    "OVERDRIVE",
    "AFTERBURN",
    "BURNOUT",
    "TAKEDOWN",
    "TAKEOFF",
    "LANDING",
    "RUNWAY",
    "JETSTREAM",
    "SKYLINE",
    "AIRBORNE",
    "JET",
    "TANK",
    "TANKER",
    "CONVOY",
    "MARINER",
    "CAPTAIN",
    "ADMIRAL",
    "COMMANDER",
    "PILOT",
    "NAVIGATOR",
    "SCOUT",
    "TRACKER",
    "SEEKER",
    "HUNTER",
    "STALKER",
    "MARKSMAN",
    "SNIPER",
    "TACTIC",
    "STRATEGY",
    "VICTORY",
    "TRIUMPH",
    "GLORY",
    "HONOR",
    "VALOR",
    "COURAGE",
    "BRAVERY",
    "MIGHT",
    "POWER",
    "FORCE",
    "ENERGY",
    "MOMENTUM",
    "KINETIC",
    "DYNAMIC",
    "STATIC",
    "CHAOS",
    "ORDER",
    "BALANCE",
    "HARMONY",
    "RHYTHM",
    "TEMPO",
    "MELODY",
    "SONATA",
    "CHORUS",
    "ECHO",
    "REVERB",
    "BASS",
    "TREBLE",
    "BEAT",
    "BONGO",
    "DRUMMER",
    "GUITAR",
    "PIANO",
    "SYNTH",
    "VINYL",
    "RETRO",
    "VINTAGE",
    "CLASSIC",
    "MODERN",
    "FUTURE",
    "FUTURIST",
    "TIMELESS",
    "INFINITE",
    "ETERNAL",
    "ENDLESS",
    "INFINITY",
    "INFINITYX",
    "INNOVATE",
    "INVENTOR",
    "MAKER",
    "BUILDER",
    "ARCHITECT",
    "DESIGNER",
    "ARTISAN",
    "CRAFT",
    "CRAFTSMAN",
    "MASTER",
    "MAESTRO",
    "PRODIGY",
    "GENIUS",
    "SAVANT",
    "WONDER",
    "MARVEL",
    "MIRACLE",
    "PHENOM",
    "OUTLAW",
    "BANDIT",
    "MAVERICK",
    "REBEL",
    "RENEGADE",
    "FREEDOM",
    "LIBERTY",
    "JUSTICE",
    "VENGEANCE",
    "REVENGE",
    "DESTINY",
    "FATE",
    "FORTUNE",
    "LUCKY",
    "BLESSED",
    "CHARM",
    "CHARMING",
    "MYSTERY",
    "WONDERLAND",
    "DREAM",
    "DREAMER",
    "VISION",
    "VISIONARY",
    "IMAGINE",
    "IMAGINER",
    "FANTASY",
    "FANTOM",
    "SILHOUETTE",
    "SHIMMER",
    "GLOW",
    "RADIANT",
    "LUMEN",
    "LUMINOUS",
    "BEACON",
    "LANTERN",
    "TORCH",
    "CANDLE",
    "FLICKER",
    "TWILIGHT",
    "DUSK",
    "DAWN",
    "DAYBREAK",
    "SUNRISE",
    "SUNSET",
    "MIDNIGHT",
    "MOONLIGHT",
    "STARLIGHT",
    "NIGHTFALL",
    "DAYLIGHT",
    "NOCTURNE"
]


def _make_spoiler_image(challenge: str) -> BufferedInputFile:
    """Draw one unique challenge word into the spoiler image."""
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
    """Normalize one-word answers, ignoring case, whitespace, and punctuation."""
    return re.sub(r"[^a-z0-9]+", "", (value or "").casefold())


def _as_utc(value: datetime) -> datetime:
    """Treat naive database datetimes as UTC and normalize aware values to UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)



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
        # Never reuse a word from any previous event, even after its event expires.
        previous_challenges = (await session.scalars(
            select(SpeedTypeEvent.challenge_text)
        )).all()
        previously_used_words = {
            token.casefold()
            for previous in previous_challenges
            for token in re.findall(r"[A-Za-z]+", previous or "")
        }
        unused_challenges = [
            item for item in CHALLENGES
            if item.casefold() not in previously_used_words
        ]
        if not unused_challenges:
            # Never silently repeat words when the unique pool is exhausted.
            return False
        challenge = random.choice(unused_challenges)
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
