from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import CommandStart, Command
from sqlalchemy.ext.asyncio import AsyncSession
from bot.config import settings
from bot.keyboards.main_menu import main_menu_kb, back_button_kb
import structlog

logger = structlog.get_logger()
router = Router()

COMMANDS_EN = (
    "👋 Hello {name}!\n\n"
    "📋 Available commands:\n\n"
    "💰 Economy:\n"
    "  /wallet — wallet and balance\n"
    "  /daily — daily reward\n"
    "  /weekly — weekly reward\n"
    "  /monthly — monthly reward\n"
    "  /transfer — transfer coins\n"
    "  /referral — referral link\n\n"
    "🎮 Games:\n"
    "  /dice — dice roll\n"
    "  /rps — rock paper scissors\n"
    "  /quiz — quiz game\n"
    "  /wheel — wheel of fortune\n"
    "  /roulette — roulette\n"
    "  /duel — duel\n\n"
    "🛡️ Group management:\n"
    "  /security — security settings\n"
    "  /settings — group settings\n"
    "  /stats — group statistics\n"
    "  /help — full command list\n\n"
    "Add ALEXA to your group and promote it to administrator for moderation and protection features."
)


def _get_commands(lang: str, name: str) -> str:
    return COMMANDS_EN.format(name=name)


def _detect_lang(tg_lang):
    return "en"


async def _send_add_group_prompt(message: Message):
    me = await message.bot.get_me()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Add me to your Group", url=f"https://t.me/{me.username}?startgroup=true")],
        [
            InlineKeyboardButton(text="🔔 Updates", url="https://t.me/alexa_bot_updats"),
            InlineKeyboardButton(text="❓ Help", callback_data="start:help"),
            InlineKeyboardButton(text="💬 Discussion", url="https://t.me/alexa_Bot_discussion"),
        ],
        [
            InlineKeyboardButton(text="⭐ Telegram Premium + Stars", callback_data="affiliate:promo"),
        ],
    ])
    caption = (
        "✨ <b>Welcome to ALEXA!</b> ✨\n\n"
        "Your all-in-one Telegram group assistant.\n"
        "🛡️ Moderation & protection • 🎮 Games • 💰 Economy\n\n"
        "<b>Quick Start</b>\n"
        "• Add ALEXA to your group and grant the required admin permissions\n"
        "• Use /help to see available commands\n\n"
        "Ready to make your group safer and more fun? Let's go!"
    )
    try:
        photos = await message.bot.get_user_profile_photos(me.id, limit=1)
        if photos.total_count and photos.photos and photos.photos[0]:
            photo = photos.photos[0][-1]
            await message.answer_photo(photo.file_id, caption=caption, reply_markup=kb, parse_mode="HTML")
            return
    except Exception as exc:
        logger.warning("Could not load ALEXA profile photo for /start", error=str(exc))
    await message.answer(caption, reply_markup=kb, parse_mode="HTML")

@router.message(CommandStart())
async def cmd_start(message: Message, _: callable, db_session: AsyncSession = None, db_user=None, **kwargs):
    if message.chat.type != "private":
        await message.answer("👋 ALEXA is active. Use /help to see available commands.")
        return
    args = message.text.split(maxsplit=1)[1] if len(message.text.split()) > 1 else ""
    if db_user:
        db_user.dm_started = True
    if args.startswith("ref_") and db_session and db_user:
        try:
            raw_referrer_id = args[4:]
            referrer_id = int(raw_referrer_id)
            if referrer_id != db_user.id:
                from bot.database.models import User
                referrer = await db_session.get(User, referrer_id)
                if referrer and not referrer.is_bot and referrer.dm_started:
                    from bot.services.economy_service import process_referral
                    await process_referral(
                        db_session,
                        referrer_id=referrer_id,
                        new_user_id=db_user.id,
                    )
                else:
                    logger.info(
                        "Referral ignored: inviter not found or has not started DM",
                        referrer_id=referrer_id,
                    )
        except (TypeError, ValueError):
            logger.info("Referral ignored: invalid start payload")
        except Exception as exc:
            logger.warning("Could not process referral start payload", error=str(exc))
            raise
    # Always show the Alexa welcome screen on private /start.
    await _send_add_group_prompt(message)
    return



@router.callback_query(F.data == "start:help")
async def start_help(callback: CallbackQuery, **kwargs):
    name = callback.from_user.first_name or "User"
    try:
        if callback.message.photo:
            await callback.message.delete()
            await callback.bot.send_message(
                callback.message.chat.id,
                _get_commands("en", name),
                reply_markup=main_menu_kb(None, is_owner=(callback.from_user.id == settings.effective_owner_id)),
            )
        else:
            await callback.message.edit_text(
                _get_commands("en", name),
                reply_markup=main_menu_kb(None, is_owner=(callback.from_user.id == settings.effective_owner_id)),
            )
    except Exception as exc:
        logger.warning("Could not open ALEXA help menu", error=str(exc))
        await callback.answer("Please use /help to see commands.", show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data == "start:open")
async def start_open_menu(callback: CallbackQuery, _: callable, **kwargs):
    if callback.message:
        chat_id = callback.message.chat.id
        try:
            await callback.message.delete()
        except Exception as exc:
            logger.info("Could not delete previous start screen", error=str(exc))
        name = callback.from_user.first_name or "User"
        await callback.bot.send_message(
            chat_id,
            _get_commands("en", name),
            reply_markup=main_menu_kb(_, is_owner=(callback.from_user.id == settings.effective_owner_id)),
        )
    await callback.answer()


@router.message(Command("menu"))
async def cmd_menu(message: Message, _: callable, db_user=None, **kwargs):
    if message.chat.type == "private" and db_user and not db_user.has_added_bot_to_group:
        await _send_add_group_prompt(message)
        return
    await message.answer(
        "🛡️ <b>ALEXA Menu</b>\nChoose a section:",
        reply_markup=main_menu_kb(_, is_owner=(message.from_user.id == settings.effective_owner_id)),
        parse_mode="HTML",
    )


@router.message(Command("language", "lang"))
async def cmd_language(message: Message, **kwargs):
    await message.answer("🌐 ALEXA currently supports English only.")


@router.message(F.text.startswith("/setlang_"))
async def set_language_cmd(message: Message, db_session: AsyncSession = None, db_user=None, **kwargs):
    if message.chat.type == "private" and db_user and not db_user.has_added_bot_to_group:
        await _send_add_group_prompt(message)
        return
    if db_user:
        db_user.language = "en"
        if db_session:
            try:
                await db_session.flush()
            except Exception:
                pass
    name = message.from_user.first_name or "User"
    await message.answer(
        "🌐 ALEXA uses English for all messages.\n\n" + _get_commands("en", name),
        reply_markup=main_menu_kb(None, is_owner=(message.from_user.id == settings.effective_owner_id)),
    )


@router.callback_query(F.data.startswith("lang:"))
async def lang_callback_fallback(callback: CallbackQuery, db_session: AsyncSession = None, db_user=None, **kwargs):
    if callback.message.chat.type == "private" and db_user and not db_user.has_added_bot_to_group:
        me = await callback.bot.get_me()
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="➕ Add ALEXA to Group", url=f"https://t.me/{me.username}?startgroup=true")
        ]])
        await callback.message.edit_text("👋 First, add ALEXA to your group. Then return here and use /start.", reply_markup=kb)
        await callback.answer()
        return
    if db_user:
        db_user.language = "en"
        if db_session:
            try:
                await db_session.flush()
            except Exception:
                pass
    name = callback.from_user.first_name or "User"
    await callback.message.edit_text(
        "🌐 ALEXA currently supports English only.\n\n" + _get_commands("en", name),
        reply_markup=main_menu_kb(None, is_owner=(callback.from_user.id == settings.effective_owner_id)),
    )
    await callback.answer()


@router.callback_query(F.data.in_({"menu:main", "menu:language", "menu:games", "menu:achievements"}))
async def menu_fallback(callback: CallbackQuery, _: callable, db_user=None, **kwargs):
    if callback.message.chat.type == "private" and db_user and not db_user.has_added_bot_to_group:
        me = await callback.bot.get_me()
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="➕ Add ALEXA to Group", url=f"https://t.me/{me.username}?startgroup=true")
        ]])
        await callback.message.edit_text("👋 First, add ALEXA to your group. Then return here and use /start.", reply_markup=kb)
        await callback.answer()
        return
    action = callback.data
    if action == "menu:language":
        await callback.message.edit_text(
            "🌐 ALEXA currently supports English only.",
            reply_markup=back_button_kb(_, "menu:main"),
        )
    elif action == "menu:games":
        from bot.keyboards.games import games_menu_kb
        await callback.message.edit_text("🎮 <b>Games</b>\nChoose a game:", reply_markup=games_menu_kb(_), parse_mode="HTML")
    elif action == "menu:achievements":
        from bot.keyboards.economy import rewards_menu_kb
        await callback.message.edit_text("🏅 <b>Achievements &amp; Rewards</b>", reply_markup=rewards_menu_kb(_), parse_mode="HTML")
    else:
        await callback.message.edit_text(
            _get_commands("en", callback.from_user.first_name or "User"),
            reply_markup=main_menu_kb(_, is_owner=(callback.from_user.id == settings.effective_owner_id)),
        )
    await callback.answer()


@router.callback_query(F.data == "group:main")
async def group_main_refresh(callback: CallbackQuery, **kwargs):
    await callback.answer("Use /help to see available group commands.")
