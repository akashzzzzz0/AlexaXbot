import io
import logging

from PIL import Image, ImageDraw, ImageFont, ImageOps

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MessageEntity,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models import IntroProfile, User

logger = logging.getLogger(__name__)
router = Router()


class IntroSetup(StatesGroup):
    nickname = State()
    about = State()
    hobbies = State()
    interests = State()
    languages = State()
    favourite = State()
    photo = State()


_STEPS = [
    ("nickname", IntroSetup.nickname, "👤 What name or nickname should appear on your card?"),
    ("about", IntroSetup.about, "📝 Tell us a little about yourself."),
    ("hobbies", IntroSetup.hobbies, "🎮 What are your hobbies?"),
    ("interests", IntroSetup.interests, "💖 What are your interests?"),
    ("languages", IntroSetup.languages, "🌍 Which languages do you speak?"),
    ("favourite", IntroSetup.favourite, "⭐ Tell us your favourite things (optional)."),
    ("photo", IntroSetup.photo, "📸 Send a profile photo, or skip to use your Telegram profile photo."),
]


def _skip_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭️ Skip this step", callback_data="intro:skip")],
        [InlineKeyboardButton(text="❌ Cancel", callback_data="intro:cancel")],
    ])


def _step_index(state_name):
    name = (state_name or "").split(":")[-1]
    for index, item in enumerate(_STEPS):
        if item[0] == name:
            return index
    return -1


async def _ask_step(message: Message, state: FSMContext, index: int):
    field, state_obj, prompt = _STEPS[index]
    await state.set_state(state_obj)
    await message.answer(prompt, reply_markup=_skip_keyboard())


async def start_intro_setup(message: Message, state: FSMContext):
    if message.chat.type != "private":
        await message.answer("Please open ALEXA in private chat to create your introduction.")
        return
    await state.clear()
    await state.update_data(intro_fields={}, intro_entities={})
    await message.answer(
        "✨ <b>Let's create your ALEXA introduction!</b>\n"
        "I'll ask a few short questions. You can skip any step and update your card later.",
        parse_mode="HTML",
    )
    await _ask_step(message, state, 0)


@router.message(Command("setintro"))
async def set_intro(message: Message, state: FSMContext):
    if message.chat.type == "private":
        await start_intro_setup(message, state)
        return
    bot_user = await message.bot.get_me()
    if not bot_user.username:
        await message.answer("I couldn't create the private setup link right now. Please try again later.")
        return
    link = f"https://t.me/{bot_user.username}?start=intro"
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✨ Create My Introduction", url=link)]
    ])
    await message.answer(
        "👤 <b>Personal Introduction</b>\n\n"
        "Create your own intro card privately with ALEXA. Tap below, press Start, and complete your profile in DM.",
        parse_mode="HTML",
        reply_markup=markup,
    )


@router.message(Command("cancel"), StateFilter(
    IntroSetup.nickname,
    IntroSetup.about,
    IntroSetup.hobbies,
    IntroSetup.interests,
    IntroSetup.languages,
    IntroSetup.favourite,
    IntroSetup.photo,
))
async def cancel_intro(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Introduction setup cancelled. You can restart it anytime with /setintro.")


@router.callback_query(F.data == "intro:cancel")
async def cancel_intro_callback(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    if callback.message:
        await callback.message.edit_text("❌ Introduction setup cancelled. Use /setintro whenever you're ready.")
    await callback.answer()


@router.callback_query(F.data == "intro:skip")
async def skip_intro_step(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession, **kwargs):
    current = await state.get_state()
    if not current or not current.startswith("IntroSetup:"):
        await callback.answer("No introduction setup is active.", show_alert=True)
        return
    index = _step_index(current)
    if index < 0:
        await callback.answer("This step is no longer active.", show_alert=True)
        return
    if index == len(_STEPS) - 1:
        await _persist_intro_profile(
            callback.from_user, callback.message, state, db_session, callback.bot
        )
        await callback.answer("Saved")
        return
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=None)
        await _ask_step(callback.message, state, index + 1)
    await callback.answer("Skipped")


@router.message(StateFilter(
    IntroSetup.nickname,
    IntroSetup.about,
    IntroSetup.hobbies,
    IntroSetup.interests,
    IntroSetup.languages,
    IntroSetup.favourite,
))
async def collect_intro_text(message: Message, state: FSMContext):
    current = await state.get_state()
    index = _step_index(current)
    if index < 0:
        return
    if not message.text:
        await message.answer("Please send text for this step, or use Skip.")
        return
    if message.text.strip().lower() in {"/skip", "skip"}:
        await _ask_step(message, state, index + 1)
        return

    field = _STEPS[index][0]
    raw_value = message.text
    value = raw_value.strip()
    # Telegram entity offsets refer to the original message, before strip().
    leading_trim_units = _utf16_length(raw_value[:len(raw_value) - len(raw_value.lstrip())])
    limit = 500 if field == "about" else 250
    if len(value) > limit:
        await message.answer(f"Please keep this answer under {limit} characters.")
        return

    custom_entities = []
    for entity in (message.entities or []):
        if getattr(entity.type, "value", entity.type) != "custom_emoji":
            continue
        entity_data = entity.model_dump(mode="json", exclude_none=True)
        # Store offsets relative to the trimmed value so they remain correct
        # when the profile is later rendered as a Telegram-native message.
        entity_data["offset"] = int(entity_data.get("offset", 0)) - leading_trim_units
        if entity_data["offset"] < 0:
            continue
        custom_entities.append(entity_data)
    if custom_entities and not bool(getattr(message.from_user, "is_premium", False)):
        await message.answer(
            "⭐ Telegram Premium Custom Emojis are available in your intro only when your Telegram account has Premium. "
            "You can use normal emojis or remove the custom emojis and send again."
        )
        return

    data = await state.get_data()
    fields = dict(data.get("intro_fields") or {})
    entities = dict(data.get("intro_entities") or {})
    fields[field] = value
    entities[field] = custom_entities
    await state.update_data(intro_fields=fields, intro_entities=entities)
    await _ask_step(message, state, index + 1)


async def _persist_intro_profile(user, reply_message: Message, state: FSMContext,
                                 db_session: AsyncSession, bot):
    if not db_session:
        await reply_message.answer("Intro profiles are temporarily unavailable. Please try again.")
        return
    data = await state.get_data()
    fields = data.get("intro_fields") or {}
    photo_file_id = data.get("intro_photo_file_id")

    profile = await db_session.get(IntroProfile, user.id)
    if profile is None:
        profile = IntroProfile(user_id=user.id)
        db_session.add(profile)

    # Keep the previous saved photo when someone updates text and skips the
    # photo step. A newly uploaded photo always takes priority.
    if not photo_file_id and profile.photo_file_id:
        photo_file_id = profile.photo_file_id

    # If this is a new profile with no uploaded/saved photo, use the current
    # Telegram profile photo as the initial avatar.
    if not photo_file_id:
        try:
            photos = await bot.get_user_profile_photos(user.id, limit=1)
            if photos.total_count and photos.photos and photos.photos[0]:
                photo_file_id = photos.photos[0][-1].file_id
        except Exception as exc:
            logger.info("Could not fetch Telegram profile photo for intro: %s", exc)
    profile.nickname = fields.get("nickname") or (user.first_name or "Member")
    profile.about = fields.get("about")
    profile.hobbies = fields.get("hobbies")
    profile.interests = fields.get("interests")
    profile.languages = fields.get("languages")
    profile.favourite = fields.get("favourite")
    profile.photo_file_id = photo_file_id
    profile.field_entities = data.get("intro_entities") or {}
    await db_session.flush()
    await state.clear()
    await reply_message.answer(
        "🎉 <b>Your introduction is saved!</b>\n\n"
        "Use /intro in a group to show your card, or /setintro anytime to update it.",
        parse_mode="HTML",
    )


@router.message(IntroSetup.photo)
async def collect_intro_photo(message: Message, state: FSMContext, db_session: AsyncSession, **kwargs):
    if message.photo:
        await state.update_data(intro_photo_file_id=message.photo[-1].file_id)
        await _persist_intro_profile(message.from_user, message, state, db_session, message.bot)
        return
    if message.text and message.text.strip().lower() in {"/skip", "skip"}:
        await _persist_intro_profile(message.from_user, message, state, db_session, message.bot)
        return
    await message.answer("Please send a photo, or tap Skip to use your Telegram profile photo.")


def _load_intro_font(size: int, bold: bool = False):
    paths = (
        ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
         "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"]
        if bold else
        ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
         "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"]
    )
    for path in paths:
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _wrap_intro_text(draw, value: str, font, max_width: int):
    words = str(value).split()
    lines = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and draw.textlength(candidate, font=font) > max_width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [""]


def _render_intro_card(profile: IntroProfile, user: User, photo_bytes=None):
    """Render the member profile and all supplied answers into one image."""
    width = 1080
    margin = 54
    inner_width = width - margin * 2
    title_font = _load_intro_font(43, True)
    name_font = _load_intro_font(48, True)
    label_font = _load_intro_font(25, True)
    body_font = _load_intro_font(27)
    small_font = _load_intro_font(21)

    nickname = (profile.nickname or "").strip() or (user.first_name or "Member")
    sections = [
        ("✦  ABOUT ME", profile.about),
        ("◆  HOBBIES", profile.hobbies),
        ("♡  INTERESTS", profile.interests),
        ("✧  LANGUAGES", profile.languages),
        ("★  FAVOURITE THINGS", profile.favourite),
    ]
    sections = [(label, str(value).strip()) for label, value in sections if value and str(value).strip()]

    # Measure the card first so long answers fit without being clipped.
    probe = Image.new("RGB", (width, 100), "#090511")
    probe_draw = ImageDraw.Draw(probe)
    section_layout = []
    height = 360
    for label, value in sections:
        lines = _wrap_intro_text(probe_draw, value, body_font, inner_width - 58)
        block_height = 76 + len(lines) * 39 + 28
        section_layout.append((label, lines, block_height))
        height += block_height + 18
    height += 100

    canvas = Image.new("RGB", (width, height), "#090511")
    draw = ImageDraw.Draw(canvas)
    # Subtle purple glow bands and a clean neon frame.
    draw.rounded_rectangle((12, 12, width - 12, height - 12), radius=42,
                           fill="#10091d", outline="#a64dff", width=5)
    draw.rounded_rectangle((27, 27, width - 27, height - 27), radius=34,
                           outline="#542080", width=2)
    draw.text((margin, 55), "ALEXA  |  PERSONAL INTRO", font=title_font, fill="#e8c7ff")

    # Integrated circular profile picture beside the name.
    avatar_box = (margin, 132, margin + 170, 302)
    draw.ellipse(avatar_box, fill="#28103f", outline="#d28aff", width=6)
    if photo_bytes:
        try:
            photo_bytes.seek(0)
            avatar = Image.open(photo_bytes).convert("RGB")
            avatar = ImageOps.fit(avatar, (158, 158), method=Image.Resampling.LANCZOS)
            mask = Image.new("L", (158, 158), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, 157, 157), fill=255)
            canvas.paste(avatar, (margin + 6, 138), mask)
            draw = ImageDraw.Draw(canvas)
            draw.ellipse((margin + 3, 135, margin + 167, 299), outline="#d28aff", width=5)
        except Exception:
            logger.exception("Could not place intro profile photo in card")

    name_x = margin + 205
    draw.text((name_x, 160), nickname, font=name_font, fill="#ffffff")
    if user.username:
        draw.text((name_x + 2, 226), f"@{user.username}", font=small_font, fill="#cda8f4")
    else:
        draw.text((name_x + 2, 226), "ALEXA MEMBER", font=small_font, fill="#cda8f4")

    y = 335
    for label, lines, block_height in section_layout:
        draw.rounded_rectangle((margin, y, width - margin, y + block_height),
                               radius=25, fill="#1b1029", outline="#9a48d7", width=3)
        draw.text((margin + 28, y + 18), label, font=label_font, fill="#d9a8ff")
        text_y = y + 60
        for line in lines:
            draw.text((margin + 30, text_y), line, font=body_font, fill="#f7efff")
            text_y += 39
        y += block_height + 18

    footer_y = height - 82
    draw.line((margin, footer_y - 12, width - margin, footer_y - 12), fill="#65318d", width=2)
    draw.text((margin, footer_y), "POWERED BY ALEXA", font=label_font, fill="#d9a8ff")
    output = io.BytesIO()
    canvas.save(output, format="PNG", optimize=True)
    output.seek(0)
    output.name = "alexa_intro_card.png"
    return output


def _utf16_length(value: str) -> int:
    """Telegram entity offsets are measured in UTF-16 code units."""
    return len(value.encode("utf-16-le")) // 2


def _utf16_slice(value: str, offset: int, length: int) -> str:
    encoded = value.encode("utf-16-le")
    return encoded[offset * 2:(offset + length) * 2].decode("utf-16-le", errors="ignore")


def _build_intro_telegram_text(profile: IntroProfile, target: User):
    """Build a Telegram-native copy with correctly rebased Premium emoji entities."""
    nickname = (profile.nickname or "").strip() or (target.first_name or "Member")
    lines = ["✨ PERSONAL INTRODUCTION ✨", f"👤 {nickname}"]
    if target.username:
        lines.append(f"🔗 Telegram: @{target.username}")
    entities = []
    saved_entities = profile.field_entities or {}
    for field, label in (
        ("about", "📝 About Me"),
        ("hobbies", "🎮 Hobbies"),
        ("interests", "💖 Interests"),
        ("languages", "🌍 Languages"),
        ("favourite", "⭐ Favourite Things"),
    ):
        value = getattr(profile, field, None)
        if not value:
            continue
        value = str(value)
        prefix = f"{label}: "
        base_offset = _utf16_length("\n".join(lines) + "\n" + prefix)
        for raw_entity in saved_entities.get(field, []) or []:
            if not isinstance(raw_entity, dict):
                continue
            if raw_entity.get("type") != "custom_emoji" or not raw_entity.get("custom_emoji_id"):
                continue
            try:
                entity_data = dict(raw_entity)
                local_offset = int(entity_data.get("offset", 0))
                local_length = int(entity_data.get("length", 0))
                value_length = _utf16_length(value)
                if local_offset < 0 or local_length <= 0 or local_offset + local_length > value_length:
                    continue
                # Telegram requires the custom-emoji entity to cover its
                # complete Unicode fallback emoji, not surrounding text.
                fallback_emoji = _utf16_slice(value, local_offset, local_length)
                if not fallback_emoji:
                    continue
                entity_data["offset"] = base_offset + local_offset
                entity = MessageEntity.model_validate(entity_data)
                entities.append(entity)
            except (TypeError, ValueError, UnicodeError):
                logger.warning("Skipping invalid saved intro custom-emoji entity")
        lines.append(prefix + value)
    lines.append("\n💜 Powered by ALEXA")
    return "\n".join(lines), entities

async def _load_intro_photo(bot, user_id: int, preferred_file_id=None):
    """Download and validate a saved or current Telegram profile photo."""
    candidate_ids = []
    if preferred_file_id:
        candidate_ids.append(preferred_file_id)

    # If a previously saved Telegram file_id is no longer downloadable, try
    # the user's latest profile photo before giving up.
    try:
        photos = await bot.get_user_profile_photos(user_id, limit=1)
        if photos.total_count and photos.photos and photos.photos[0]:
            latest_file_id = photos.photos[0][-1].file_id
            if latest_file_id and latest_file_id not in candidate_ids:
                candidate_ids.append(latest_file_id)
    except Exception as exc:
        logger.info("Could not look up current Telegram profile photo: %s", exc)

    for file_id in candidate_ids:
        try:
            telegram_file = await bot.get_file(file_id)
            if not telegram_file.file_path:
                raise ValueError("Telegram returned an empty photo file path")
            photo_bytes = io.BytesIO()
            await bot.download_file(telegram_file.file_path, destination=photo_bytes)
            photo_bytes.seek(0)
            # Validate the downloaded bytes before passing them to Pillow.
            with Image.open(photo_bytes) as image:
                image.verify()
            photo_bytes.seek(0)
            return file_id, photo_bytes
        except Exception as exc:
            logger.warning("Could not download intro photo %s: %s", file_id, exc)

    return (candidate_ids[-1] if candidate_ids else None), None


async def _show_intro(message: Message, target: User, db_session: AsyncSession):
    profile = await db_session.get(IntroProfile, target.id)
    if not profile:
        await message.answer("👤 This member hasn't created an introduction yet.")
        return

    photo_file_id, photo_bytes = await _load_intro_photo(
        message.bot, target.id, profile.photo_file_id
    )
    telegram_text, telegram_entities = _build_intro_telegram_text(profile, target)

    card_sent = False
    try:
        card = _render_intro_card(profile, target, photo_bytes)
        await message.answer_photo(photo=card)
        card_sent = True
    except Exception:
        logger.exception("Could not render intro card image")

    # If the card could not embed the avatar, still show the original Telegram
    # photo as a separate image instead of silently dropping it.
    if photo_file_id and (not card_sent or photo_bytes is None):
        try:
            await message.answer_photo(photo=photo_file_id, caption="📸 Profile photo")
        except Exception as exc:
            logger.warning("Could not send intro photo fallback: %s", exc)

    if not card_sent:
        if telegram_entities:
            try:
                await message.answer(
                    telegram_text,
                    entities=telegram_entities,
                    disable_web_page_preview=True,
                )
                return
            except Exception:
                logger.exception("Telegram rejected custom emoji entities in intro fallback")
        await message.answer(telegram_text, disable_web_page_preview=True)
        return

    # Pillow cannot draw Telegram Premium custom emoji. Telegram-native message
    # entities are required for those emojis to render.
    if telegram_entities:
        try:
            await message.answer(
                telegram_text,
                entities=telegram_entities,
                disable_web_page_preview=True,
            )
        except Exception:
            logger.exception("Telegram rejected custom emoji entities in intro output")
            await message.answer(telegram_text, disable_web_page_preview=True)


@router.message(Command("intro"))
async def show_intro(message: Message, db_session: AsyncSession, **kwargs):
    if not db_session:
        await message.answer("Intro profiles are temporarily unavailable. Please try again.")
        return
    target = None
    args = (message.text or "").split(maxsplit=1)
    if message.reply_to_message and message.reply_to_message.from_user:
        target = await db_session.get(User, message.reply_to_message.from_user.id)
    elif len(args) > 1 and args[1].strip().startswith("@"):
        username = args[1].strip().split()[0].lstrip("@").lower()
        target = await db_session.scalar(select(User).where(func.lower(User.username) == username))
    else:
        target = await db_session.get(User, message.from_user.id)
    if not target:
        await message.answer("I couldn't find that member in my saved group records.")
        return
    await _show_intro(message, target, db_session)


@router.message(Command("delintro"))
async def delete_intro(message: Message, db_session: AsyncSession, **kwargs):
    profile = await db_session.get(IntroProfile, message.from_user.id) if db_session else None
    if not profile:
        await message.answer("You don't have a saved introduction yet.")
        return
    await db_session.delete(profile)
    await message.answer("🗑️ Your introduction has been deleted.")
