from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import CommandStart, Command
from sqlalchemy.ext.asyncio import AsyncSession
from bot.config import settings
from bot.keyboards.main_menu import main_menu_kb, back_button_kb
import structlog
from html import escape, unescape
import re

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
    "  /ludo — Ludo race against Alexa\n"
    "  /bingo — Bingo number draw\n"
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
        [InlineKeyboardButton(text="👑 Premium · 3 Months + ⭐ Stars", callback_data="affiliate:promo")],
        [InlineKeyboardButton(text="🎁 My Referral Rewards", callback_data="affiliate:open")],
        [
            InlineKeyboardButton(text="🎮 Games & Economy", callback_data="menu:games"),
            InlineKeyboardButton(text="🛡️ Group Security", callback_data="menu:security"),
        ],
        [InlineKeyboardButton(text="🤖 AI Assistant", callback_data="menu:main")],
        [InlineKeyboardButton(text="📚 Help & Commands", callback_data="start:help")],
        [InlineKeyboardButton(text="➕ Add Me to Your Group", url=f"https://t.me/{me.username}?startgroup=true")],
        [InlineKeyboardButton(text="💬 Discussion Group", url="https://t.me/+dGq9_BqdD5hjYzk0")],
        [InlineKeyboardButton(text="📢 Updates Channel", url="https://t.me/+X5aelUHksVRlODQ8")],
        [InlineKeyboardButton(text="📣 Free Group Promotion", callback_data="start:promotion")],
    ])
    user = message.from_user
    first_name = escape(user.first_name or "there") if user else "there"
    username_line = f"👤 @{escape(user.username)}\n" if user and user.username else ""
    caption = (
        f'<tg-emoji emoji-id="5368324170671202285">⭐</tg-emoji> '
        f'<b>Welcome, {first_name}!</b> '
        f'<tg-emoji emoji-id="5472164874886846699">✨</tg-emoji>\n'
        f'{username_line}\n'
        '<tg-emoji emoji-id="5372981976804366741">🤖</tg-emoji> <b>ALEXA — YOUR ALL-IN-ONE GROUP ASSISTANT</b>\n\n'
        '<tg-emoji emoji-id="5431577498364158238">📊</tg-emoji> <b>ADVANCED GROUP SECURITY</b>\n'
        '🛡️ Anti-spam • Link protection • Smart locks\n'
        '⚖️ Moderation • Admin tools • Member management\n\n'
        '<tg-emoji emoji-id="5409008750893734809">🏆</tg-emoji> <b>ALEXA WORLD ARENA</b>\n'
        '🎮 Games • Quizzes • Battles • Missions\n'
        '🏅 XP • Ranks • Badges • Leaderboards\n\n'
        '<tg-emoji emoji-id="5379600444098093058">🪙</tg-emoji> <b>ECONOMY & REWARDS</b>\n'
        '💰 Wallet • Daily rewards • Referral system\n\n'
        '<tg-emoji emoji-id="5471952986970267163">💎</tg-emoji> <b>PREMIUM & STARS</b>\n'
        '⭐ 3-month Telegram Premium request option\n'
        '🎁 Stars referral rewards and payment support\n\n'
        '<tg-emoji emoji-id="5472164874886846699">✨</tg-emoji> <b>AI SMART TOOLS</b>\n'
        '🤖 AI assistant • Rewrite • Translate • Summarize\n\n'
        '<b>QUICK START</b>\n'
        '➕ Add ALEXA to your group and grant required admin permissions.\n'
        '📋 Use /help to explore commands.\n\n'
        '<tg-emoji emoji-id="5436040291507247633">🎉</tg-emoji> <b>Let’s make your group safer and more fun!</b>'
    )
    # Use ordinary Unicode emoji and plain text. This avoids invalid custom-emoji
    # entities and ensures /start does not depend on a profile-photo/document ID.
    safe_caption = re.sub(r'<tg-emoji[^>]*>(.*?)</tg-emoji>', lambda match: match.group(1), caption)
    safe_caption = re.sub(r'</?b>', '', safe_caption)
    safe_caption = unescape(safe_caption)
    safe_caption = re.sub(r'<[^>]+>', '', safe_caption)

    try:
        # Reuse ALEXA's current Telegram profile photo when available.
        photos = await message.bot.get_user_profile_photos(me.id, limit=1)
        if photos.total_count and photos.photos:
            profile_photo = photos.photos[0][-1].file_id
            # Keep the interactive keyboard on a text message so all legacy
            # callback handlers continue to work with their existing behavior.
            await message.answer_photo(photo=profile_photo)
        await message.answer(safe_caption, reply_markup=kb, parse_mode=None)
    except Exception as exc:
        logger.warning("Could not send ALEXA welcome with profile photo; retrying plain text", error=str(exc))
        # Last-resort fallback preserves the existing welcome text.
        try:
            await message.answer(safe_caption, reply_markup=kb, parse_mode=None)
        except Exception as fallback_exc:
            logger.warning("Could not send ALEXA welcome with buttons", error=str(fallback_exc))
            await message.answer(safe_caption, parse_mode=None)

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
async def start_help(callback: CallbackQuery, _: callable, **kwargs):
    try:
        from bot.handlers.help import build_help_text
        help_text = build_help_text(_)
        if callback.message.photo:
            await callback.message.delete()
            await callback.bot.send_message(
                callback.message.chat.id,
                help_text,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🧭 Help Sections", callback_data="menu:help")],
                    [InlineKeyboardButton(text="🏠 Main Menu", callback_data="menu:main")],
                ]),
            )
        else:
            await callback.message.edit_text(
                help_text,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🧭 Help Sections", callback_data="menu:help")],
                    [InlineKeyboardButton(text="🏠 Main Menu", callback_data="menu:main")],
                ]),
            )
    except Exception as exc:
        logger.warning("Could not open ALEXA help menu", error=str(exc))
        await callback.answer("Please use /help to see commands.", show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data == "start:promotion")
async def start_group_promotion(callback: CallbackQuery, **kwargs):
    text = (
        "📣 <b>FREE GROUP PROMOTION</b>\n\n"
        "Promote your Telegram group through ALEXA.\n\n"
        "1. Add ALEXA to your Telegram group.\n"
        "2. Give ALEXA the required administrator permissions.\n"
        "3. Return to this DM and send <code>.done</code>.\n"
        "4. Send your group link to ALEXA in this private chat.\n\n"
        "ALEXA must be able to verify the group before a promotion can be reviewed."
    )
    try:
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Add Me to Your Group", url=f"https://t.me/{(await callback.bot.get_me()).username}?startgroup=true")],
            [InlineKeyboardButton(text="◀️ Back to Commands", callback_data="start:open")],
        ])
        if callback.message.photo:
            await callback.message.edit_caption(text, parse_mode="HTML", reply_markup=markup)
        else:
            await callback.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
    except Exception as exc:
        logger.warning("Could not open group promotion instructions", error=str(exc))
        await callback.answer("Please open /start again.", show_alert=True)
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
