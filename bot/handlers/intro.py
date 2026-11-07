import logging

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
    value = message.text.strip()
    limit = 500 if field == "about" else 250
    if len(value) > limit:
        await message.answer(f"Please keep this answer under {limit} characters.")
        return

    custom_entities = [
        entity.model_dump(mode="json", exclude_none=True)
        for entity in (message.entities or [])
        if getattr(entity.type, "value", entity.type) == "custom_emoji"
    ]
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

    if not photo_file_id:
        try:
            photos = await bot.get_user_profile_photos(user.id, limit=1)
            if photos.total_count and photos.photos:
                photo_file_id = photos.photos[0][-1].file_id
        except Exception as exc:
            logger.info("Could not fetch Telegram profile photo for intro: %s", exc)

    profile = await db_session.get(IntroProfile, user.id)
    if profile is None:
        profile = IntroProfile(user_id=user.id)
        db_session.add(profile)
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


def _utf16_len(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _build_intro_card(profile: IntroProfile, user: User):
    parts = ["✨ PERSONAL INTRODUCTION ✨\n"]
    entities = []
    nickname = profile.nickname or user.first_name or "Member"
    nickname_offset = _utf16_len("✨ PERSONAL INTRODUCTION ✨\n👤 ")
    parts.append("👤 " + nickname + "\n")
    for raw in (profile.field_entities or {}).get("nickname", []):
        try:
            entity = MessageEntity.model_validate(raw)
            entity.offset += nickname_offset
            entities.append(entity)
        except Exception:
            continue

    if user.username:
        username_label = "@" + user.username
        prefix = "🔗 Telegram: "
        offset = _utf16_len("".join(parts) + prefix)
        parts.append(prefix + username_label + "\n")
        entities.append(MessageEntity(
            type="text_link", offset=offset, length=_utf16_len(username_label),
            url=f"tg://user?id={user.id}",
        ))
    else:
        parts.append(f"🆔 Telegram ID: {user.id}\n")

    labels = [
        ("about", "📝 About Me"),
        ("hobbies", "🎮 Hobbies"),
        ("interests", "💖 Interests"),
        ("languages", "🌍 Languages"),
        ("favourite", "⭐ Favourite Things"),
    ]
    for field, label in labels:
        value = getattr(profile, field, None)
        if not value:
            continue
        prefix = label + ": "
        offset = _utf16_len("".join(parts) + prefix)
        parts.append(prefix + value + "\n")
        for raw in (profile.field_entities or {}).get(field, []):
            try:
                entity = MessageEntity.model_validate(raw)
                entity.offset += offset
                entities.append(entity)
            except Exception:
                continue
    parts.append("\n💜 Powered by ALEXA")
    return "".join(parts), entities


async def _show_intro(message: Message, target: User, db_session: AsyncSession):
    profile = await db_session.get(IntroProfile, target.id)
    if not profile:
        await message.answer("👤 This member hasn't created an introduction yet.")
        return
    card_text, entities = _build_intro_card(profile, target)
    if profile.photo_file_id:
        try:
            await message.answer_photo(photo=profile.photo_file_id)
        except Exception as exc:
            logger.info("Could not send saved intro photo: %s", exc)
    try:
        await message.answer(card_text, entities=entities or None, parse_mode=None, disable_web_page_preview=True)
    except Exception as exc:
        # Telegram requires the bot owner's Premium subscription (or eligible
        # Fragment usernames) to send custom emoji entities. Keep readable text
        # available if Telegram rejects those entities.
        logger.warning("Could not send intro entities; retrying plain text: %s", exc)
        await message.answer(card_text, parse_mode=None, disable_web_page_preview=True)


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
