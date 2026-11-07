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
from sqlalchemy.exc import IntegrityError

from bot.config import settings
from bot.database.connection import async_session_maker
from bot.database.models import Group, SpeedTypeEvent, SpeedTypeSubmission
from bot.services.economy_service import add_coins

router = Router()
EVENT_INTERVAL_SECONDS = 4 * 60 * 60
EVENT_DURATION_MINUTES = 180
WINNER_REWARD = 10
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
    "NOCTURNE",
    "APPLE",
    "BANANA",
    "ORANGE",
    "GRAPES",
    "CHERRY",
    "PEACH",
    "MANGO",
    "LEMON",
    "MELON",
    "PAPAYA",
    "COCONUT",
    "CARROT",
    "POTATO",
    "TOMATO",
    "ONION",
    "GARLIC",
    "PEPPER",
    "CUCUMBER",
    "PUMPKIN",
    "SPINACH",
    "BREAD",
    "BUTTER",
    "CHEESE",
    "COOKIE",
    "BISCUIT",
    "WAFFLE",
    "PANCAKE",
    "NOODLE",
    "PASTA",
    "BURGER",
    "SANDWICH",
    "PIZZA",
    "POPCORN",
    "CHOCOLATE",
    "VANILLA",
    "CARAMEL",
    "HONEY",
    "SUGAR",
    "COFFEE",
    "TEA",
    "JUICE",
    "WATER",
    "BOTTLE",
    "CUPCAKE",
    "KETTLE",
    "SPOON",
    "PLATE",
    "WINDOW",
    "DOORWAY",
    "CEILING",
    "FLOOR",
    "GARDEN",
    "FLOWER",
    "SUNFLOWER",
    "TULIP",
    "ROSE",
    "LILY",
    "DAISY",
    "FOREST",
    "RIVER",
    "OCEAN",
    "ISLAND",
    "BEACH",
    "MOUNTAIN",
    "VALLEY",
    "MEADOW",
    "RAINBOW",
    "CLOUD",
    "BREEZE",
    "SUNSHINE",
    "RAINDROP",
    "SNOWFLAKE",
    "THUNDERBOLT",
    "UMBRELLA",
    "BACKPACK",
    "NOTEBOOK",
    "PENCIL",
    "ERASER",
    "RULER",
    "CRAYON",
    "PAINTING",
    "CAMERA",
    "MIRROR",
    "PILLOW",
    "BLANKET",
    "CURTAIN",
    "CARPET",
    "BICYCLE",
    "SCOOTER",
    "AIRPLANE",
    "TRAIN",
    "ROCKETSHIP",
    "COMPASS",
    "CALENDAR",
    "CLOCK",
    "KEYBOARD",
    "HEADPHONE",
    "SPEAKER",
    "LAPTOP",
    "TELESCOPE",
    "KEYCHAIN",
]


def _make_spoiler_image(challenge: str) -> BufferedInputFile:
    """Create a simple image containing only the challenge word."""
    width, height = 1200, 800
    image = Image.new("RGB", (width, height), (238, 242, 255))
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 112)
    except OSError:
        font = ImageFont.load_default()

    text_box = draw.textbbox((0, 0), challenge, font=font)
    text_width = text_box[2] - text_box[0]
    if text_width > width - 120:
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 76)
        except OSError:
            font = ImageFont.load_default()
        text_box = draw.textbbox((0, 0), challenge, font=font)
        text_width = text_box[2] - text_box[0]
    draw.text(
        ((width - text_width) / 2, height / 2),
        challenge,
        font=font,
        fill=(27, 35, 68),
        anchor="lm",
    )
    output = io.BytesIO()
    image.save(output, format="PNG", optimize=True)
    return BufferedInputFile(output.getvalue(), filename="alexa_speed_type_spoiler.png")


def _event_caption() -> str:
    return (
        "Be the first to write the word\n"
        "Show in the photo to climb the mini game leaderboard"
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


async def _create_event(
    bot,
    chat_id: int,
    chat_title: str,
    created_by: int | None = None,
    reserved_words: set[str] | None = None,
) -> bool:
    now = datetime.now(timezone.utc)
    async with async_session_maker() as session:
        if await _has_active_event(session, chat_id, now):
            return False
        # Avoid repeating words in this group until its 500-word bank cycles.
        previous_challenges = (await session.scalars(
            select(SpeedTypeEvent.challenge_text).where(
                SpeedTypeEvent.chat_id == chat_id
            )
        )).all()
        previously_used_words = {
            token.casefold()
            for previous in previous_challenges
            for token in re.findall(r"[A-Za-z]+", previous or "")
        }
        reserved = reserved_words if reserved_words is not None else set()
        unused_challenges = [
            item for item in CHALLENGES
            if item.casefold() not in previously_used_words
            and item.casefold() not in reserved
        ]
        if not unused_challenges:
            unused_challenges = [
                item for item in CHALLENGES if item.casefold() not in reserved
            ]
        if not unused_challenges:
            return False
        challenge = random.choice(unused_challenges)
        reserved.add(challenge.casefold())
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
        try:
            async with session.begin_nested():
                session.add(event)
                await session.flush()
        except IntegrityError:
            # Another scheduler worker already created this group's active event.
            return False
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
    # Send the first event immediately, then repeat every four hours for active groups.
    while True:
        try:
            await _close_expired_events(bot)
            async with async_session_maker() as session:
                groups = (await session.scalars(select(Group).where(Group.is_active.is_(True)))).all()
                targets = [(group.id, group.title) for group in groups]
                active_words = (await session.scalars(
                    select(SpeedTypeEvent.challenge_text).where(
                        SpeedTypeEvent.status == "active",
                        SpeedTypeEvent.ends_at > datetime.now(timezone.utc),
                    )
                )).all()
            cycle_words: set[str] = {word.casefold() for word in active_words}
            for chat_id, title in targets:
                try:
                    await _create_event(bot, chat_id, title, reserved_words=cycle_words)
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
        .execution_options(synchronize_session=False)
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
        "🎉 The challenge is now closed. Next event arrives in the next 4-hour cycle!",
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
