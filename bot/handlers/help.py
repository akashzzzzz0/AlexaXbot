from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from bot.keyboards.main_menu import back_button_kb
import structlog

logger = structlog.get_logger()
router = Router()


def build_help_text(_: callable) -> str:
    """Build the complete command list using the i18n callable."""
    return (
        f"❓ <b>{_('help_center_title')}</b>\n"
        f"💰 <b>{_('help_section_economy')}</b>\n"
        f"/wallet — {_('help_cmd_wallet')}\n"
        "/balance /economy /eco — wallet/economy aliases\n"
        f"/daily — {_('help_cmd_daily')}\n"
        f"/weekly — {_('help_cmd_weekly')}\n"
        f"/monthly — {_('help_cmd_monthly')}\n"
        f"/deposit — {_('help_cmd_deposit')}\n"
        f"/withdraw — {_('help_cmd_withdraw')}\n"
        f"/transfer — {_('help_cmd_transfer')}\n"
        f"/referral — {_('help_cmd_referral')}\n"
        "/ref — referral alias\n"
        f"/achievements — {_('help_cmd_achievements')}\n"
        "/transactions /tx — transaction history\n\n"
        f"🎮 <b>{_('help_section_games')}</b>\n"
        f"/dice — {_('help_cmd_dice')}\n"
        f"/rps — {_('help_cmd_rps')}\n"
        f"/quiz — {_('help_cmd_quiz')}\n"
        f"/wheel — {_('help_cmd_wheel')}\n"
        "/games — Open games menu\n"
        f"/numwar — {_('help_cmd_numwar')}\n"
        f"/cards — {_('help_cmd_cards')}\n"
        f"/treasure — {_('help_cmd_treasure')}\n"
        f"/mines — {_('help_cmd_mines')}\n"
        f"/roulette — {_('help_cmd_roulette')}\n"
        "/rullet — alias for roulette\n"
        "/ludo — Ludo race against Alexa\n"
        "/bingo — Bingo number draw\n\n"
        f"🛡️ <b>{_('help_section_security')}</b>\n"
        f"/security — {_('help_cmd_security')}\n"
        f"/lock — {_('help_cmd_lock')}\n"
        f"/unlock — {_('help_cmd_unlock')}\n"
        "/locks /locktypes — list content locks\n"
        "/setflood /flood /setfloodmode /clearflood — flood controls\n\n"
        f"🔨 <b>{_('help_section_moderation')}</b>\n"
        f"/ban — {_('help_cmd_ban')}\n"
        f"/unban — {_('help_cmd_unban')}\n"
        f"/kick — {_('help_cmd_kick')}\n"
        f"/mute — {_('help_cmd_mute')}\n"
        f"/unmute — {_('help_cmd_unmute')}\n"
        f"/warn — {_('help_cmd_warn')}\n"
        f"/unwarn — {_('help_cmd_unwarn')}\n"
        f"/warns — {_('help_cmd_warns')}\n"
        f"/history — {_('help_cmd_history')}\n"
        "/purg — bulk delete from a replied-to message through the command\n"
        "/promote /demote — manage group admins\n"
        "/warnings /resetwarns /del /pin /unpin — moderation tools\n"
        "/role /setrole /clearrole — custom member labels\n\n"
        "🤖 <b>Automation</b>\n"
        "/filter /stopfilter — keyword auto-replies\n"
        "/save /get /clear — saved text messages\n"
        "/blocklist /addblocklist /rmblocklist — blocked words\n"
        "/tagall &lt;message&gt; — mention all saved members with your message\n"
        "/admin &lt;message&gt; — mention group admins with your message\n"
        "@tagall &lt;message&gt; — tagall text alias\n"
        "/rules /setrules /report /admins /id /info — group utilities\n"
        "/disablecmd /enablecmd /disabledcmds — command controls\n"
        "/approve /unapprove /approvebio — member and bio approvals\n"
        "/note /autodelete /setwelcome /cancel — extra group tools\n"
        "/grouppromo — admin announcement/promotion in this group\n\n"
        "🪙 <b>Virtual games</b>\n"
        "/bal /kill /rob /revive /protect /unprotect /rank /leaders\n"
        "/items /task /bomb /bluff /card /hack /toprich /topkill /toprob /pfp\n"
        "/duel — Challenge another player\n\n"
        "🛡️ <b>Community Shield & Scam Review</b>\n"
        "/shield /shieldreport /shieldcheck — Community Shield tools\n"
        "/scamreview /scamreviews /scamresolve — scam review queue\n\n"
        "🏟️ <b>World Arena & Wars</b>\n"
        "/arena /play /arenadaily /myrank /worldtop /grouprank /worldgroups /battle\n"
        "/war /warregister /warleave /warmission /waranswer /warboard\n"
        "/warchallenge /waraccept /warresult /worldrank /mystery\n\n"
        "🧰 <b>More group tools</b>\n"
        "/approve /unapprove /approvebio /note /autodelete /setwelcome\n"
        "/resetwarn — warning reset alias\n"
        "/empirecreate — Create a group Empire (admins)\n\n"
        f"⚙️ <b>{_('help_section_settings')}</b>\n"
        f"/settings — {_('help_cmd_settings')}\n\n"
        f"ℹ️ <b>{_('help_section_other')}</b>\n"
        f"/language — {_('help_cmd_language')}\n"
        "/lang — language alias\n"
        f"/stats — {_('help_cmd_stats')}\n"
        "/gstats — group statistics alias\n"
        f"/help — {_('help_cmd_help')}\n\n"
        "🌌 <b>Ultra VIP Package</b>\n/empire — Global Empire ranking\n"
        "/empirecreate — Create an Empire (group admins)\n/empirecontribute — Add daily Empire XP\n"
        "/vip — Elite Club profile\n/vault — Open daily Legendary Vault\n/badges — View your collection\n\n"
        f"<i>{_('help_hint')}</i>"
    )

 
def build_public_help_text(_: callable) -> str:
    """Games-only command guide for ordinary group members."""
    return (
        "🎮 <b>ALEXA Games Commands</b>\n\n"
        "🎲 <b>Classic Games</b>\n"
        "/games — Open games menu\n"
        "/dice — Play dice\n/rps — Rock Paper Scissors\n"
        "/quiz — Play quiz\n/wheel — Spin the wheel\n"
        "/numwar — Play Number War\n/cards — Play card game\n"
        "/card — Card game alias\n/uno — Multiplayer UNO (group game; cards in DM)\n"
        "/treasure — Treasure hunt\n/mines — Play Mines\n"
        "/roulette — Play roulette\n/rullet — Roulette alias\n"
        "/ludo — Ludo race\n/bingo — Bingo\n/duel — Challenge a player\n\n"
        "🪙 <b>Virtual Games & Player Stats</b>\n"
        "/bal — Check game balance\n/kill — Virtual game action\n"
        "/rob — Try a virtual robbery\n/revive — Revive game profile\n"
        "/protect — Buy virtual protection\n/unprotect — Remove protection\n"
        "/rank — Your rank\n/leaders — Player leaderboard\n"
        "/topkill — Top kill scores\n/toprich — Richest players\n"
        "/toprob — Top robbers\n/items — Game items\n/task — Game tasks\n"
        "/bomb — Bomb game\n/bluff — Bluff game\n/hack — Hack game\n"
        "/pfp — View profile photo\n\n"
        "🏟️ <b>World Arena & Challenges</b>\n"
        "/arena — Open World Arena\n/play — Play an Arena challenge\n"
        "/arenadaily — Claim Arena daily reward\n/myrank — Arena rank\n"
        "/worldtop — World leaderboard\n/grouprank — Group rank\n"
        "/worldgroups — Group leaderboard\n/battle — Challenge a player\n"
        "/war /warregister /warleave — War participation\n"
        "/warmission /waranswer /warboard — War missions and board\n"
        "/warchallenge /waraccept /warresult — War challenges and results\n"
        "/worldrank /mystery — World rank and Mystery game\n"
        "/empire — Empire ranking\n/empirecontribute — Contribute Empire XP\n"
        "/vip /pro — VIP status\n/vault — Legendary Vault\n/badges — View badges\n\n"
        "/help — Show the games command guide\n"
        "Use /help in Alexa's private chat for the complete command guide."
    )


async def _is_group_admin(bot, chat, user_id: int) -> bool:
    if chat.type not in ("group", "supergroup"):
        return False
    try:
        member = await bot.get_chat_member(chat.id, user_id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


async def _has_full_help_access(bot, chat, user_id: int) -> bool:
    # In private chat, and for the configured bot owner, show the full catalogue.
    from bot.config import settings
    if chat.type == "private" or user_id == settings.effective_owner_id:
        return True
    return await _is_group_admin(bot, chat, user_id)


def help_menu_kb(is_admin: bool = False) -> InlineKeyboardMarkup:
    rows = []
    if is_admin:
        rows.append([
            InlineKeyboardButton(text="🛡️ Moderation", callback_data="help:moderation"),
            InlineKeyboardButton(text="🔒 Security", callback_data="help:security"),
        ])
    rows.append([InlineKeyboardButton(text="🎮 Games", callback_data="help:games")])
    if is_admin:
        rows.append([InlineKeyboardButton(text="💰 Economy", callback_data="help:economy")])
    rows.extend([
        [InlineKeyboardButton(text="📋 All Commands", callback_data="help:commands")],
        [InlineKeyboardButton(text="🏠 Main Menu", callback_data="menu:main")],
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


HELP_GUIDES = {
    "help:moderation": (
        "🛡️ <b>Moderation Guide</b>\n\n"
        "/ban — ban a member\n/unban — remove a ban\n/kick — remove a member\n"
        "/mute — mute a member\n/unmute — unmute a member\n"
        "/warn — issue a warning\n/warns or /warnings — view warnings\n"
        "/resetwarns — reset warnings\n/promote /demote — manage admins\n"
        "/del — delete a replied-to message\n/purg — bulk delete from a replied-to message through the command\n/pin /unpin — manage pinned messages\n\n"
        "Reply to a member's message when a command needs a target. "
        "ALEXA needs the relevant admin permissions to perform moderation actions."
    ),
    "help:security": (
        "🔒 <b>Security Guide</b>\n\n"
        "/security — view security controls\n"
        "/lock and /unlock — lock or unlock supported content types\n"
        "/locks /locktypes — view lock types\n"
        "/setflood /flood /setfloodmode /clearflood — configure flood protection\n"
        "Bio Link Protection is a separate group setting where available. "
        "ALEXA needs administrator rights and permission to delete messages for enforcement."
    ),
    "help:games": (
        "🎮 <b>Games Guide</b>\n\n"
        "/dice — dice game\n/rps — rock-paper-scissors\n/quiz — quiz\n"
        "/wheel — wheel of fortune\n/numwar — number war\n/cards — card game\n/uno — multiplayer UNO with private cards\n"
        "/treasure — treasure game\n/mines — mines game\n/roulette — roulette\n"
        "/bal /kill /rob /revive /protect /unprotect /rank /leaders — virtual game tools\n\n"
        "Use a game's command in the chat where you want to play."
    ),
    "help:economy": (
        "💰 <b>Economy Guide</b>\n\n"
        "/wallet — view your wallet\n/daily — claim a daily reward\n"
        "/weekly — claim a weekly reward\n/monthly — claim a monthly reward\n"
        "/deposit and /withdraw — manage supported wallet actions\n"
        "/transfer — transfer coins\n/referral — get your referral link\n"
        "/achievements — view achievements and rewards\n\n"
        "Rewards and balances are virtual bot features, not real money."
    ),
}


@router.message(Command('help'))
async def cmd_help(message: Message, _: callable, **kwargs):
    is_admin = await _has_full_help_access(message.bot, message.chat, message.from_user.id)
    text = build_help_text(_) if is_admin else build_public_help_text(_)
    await message.answer(
        text,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🧭 Help Sections", callback_data="menu:help")],
            [InlineKeyboardButton(text="🏠 Main Menu", callback_data="menu:main")],
        ]),
    )


@router.callback_query(F.data == "menu:help")
async def menu_help(callback: CallbackQuery, **kwargs):
    is_admin = await _has_full_help_access(callback.bot, callback.message.chat, callback.from_user.id)
    # Remove the temporary /start welcome/menu and show role-appropriate help.
    try:
        await callback.message.delete()
    except Exception as exc:
        logger.debug("Could not delete previous start menu for help", error=str(exc))
    await callback.message.answer(
        "❓ <b>ALEXA Help Center</b>\nChoose a section below.",
        parse_mode='HTML',
        reply_markup=help_menu_kb(is_admin=is_admin),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("help:"))
async def help_section(callback: CallbackQuery, _: callable, **kwargs):
    section = callback.data
    is_admin = await _has_full_help_access(callback.bot, callback.message.chat, callback.from_user.id)
    if section in ("help:moderation", "help:security") and not is_admin:
        await callback.answer("Admin help is visible only to group admins.", show_alert=True)
        return
    if section == "help:commands":
        text = build_help_text(_) if is_admin else build_public_help_text(_)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Back to Help", callback_data="menu:help")],
            [InlineKeyboardButton(text="🏠 Main Menu", callback_data="menu:main")],
        ])
    else:
        text = HELP_GUIDES.get(section)
        if not text:
            await callback.answer("This help section is not available.", show_alert=True)
            return
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 All Commands", callback_data="help:commands")],
            [InlineKeyboardButton(text="◀️ Back to Help", callback_data="menu:help")],
            [InlineKeyboardButton(text="🏠 Main Menu", callback_data="menu:main")],
        ])
    await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)
    await callback.answer()
