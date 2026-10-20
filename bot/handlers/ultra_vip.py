"""ALEXA Ultra VIP, Empire and Legendary Vault commands."""
from html import escape
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession
from bot.database.models import EmpireGuild
from bot.services.ultra_vip import BADGES, contribute_to_empire, create_empire, empire_ranking, get_elite_profile, list_badges, open_legendary_vault, vip_tier

router = Router()

async def _is_group_admin(message: Message) -> bool:
    if message.chat.type not in ("group", "supergroup") or not message.from_user: return False
    try:
        member = await message.bot.get_chat_member(message.chat.id, message.from_user.id)
        status = str(getattr(member.status, "value", member.status))
        return status in ("administrator", "creator")
    except Exception:
        return False

@router.message(Command("empire"))
async def empire_home(message: Message, db_session: AsyncSession = None, **kwargs):
    if db_session is None:
        await message.reply("🌌 Empire system is temporarily unavailable."); return
    if message.chat.type in ("group", "supergroup"):
        empire = await db_session.get(EmpireGuild, message.chat.id)
        if empire and empire.is_active:
            await message.reply(f"🌌 <b>{escape(empire.title)} Empire</b>\n👑 Level: <b>{empire.level}</b>\n"
                f"✨ XP: <b>{empire.xp}/{empire.level * 100}</b>\n\n"
                "Members can use /empirecontribute once per UTC day. Use /empire in private chat for global ranking.",
                parse_mode="HTML"); return
    rows = await empire_ranking(db_session)
    if not rows:
        await message.reply("🌌 No Empires yet. Group admins can create one with /empirecreate."); return
    lines = ["🌌 <b>ALEXA MULTIVERSE EMPIRE</b>", "🏆 Global Empire Ranking", ""]
    medals = ("🥇", "🥈", "🥉")
    for i, empire in enumerate(rows, 1):
        lines.append(f"{medals[i-1] if i <= 3 else str(i)+'.'} <b>{escape(empire.title)}</b> — Level {empire.level} · {empire.xp} XP")
    await message.reply("\n".join(lines), parse_mode="HTML")

@router.message(Command("empirecreate"))
async def empire_create_command(message: Message, db_session: AsyncSession = None, **kwargs):
    if message.chat.type not in ("group", "supergroup"):
        await message.reply("Use /empirecreate inside the group you want to turn into an Empire."); return
    if not await _is_group_admin(message):
        await message.reply("🔒 Only this group's admins can create its Empire."); return
    if db_session is None or not message.from_user:
        await message.reply("Empire setup is temporarily unavailable."); return
    try:
        empire = await create_empire(db_session, group_id=message.chat.id,
            title=message.chat.title or "Telegram Group", admin_id=message.from_user.id)
        await db_session.commit()
    except Exception:
        await db_session.rollback()
        await message.reply("⚠️ Empire could not be saved. Please try again."); return
    await message.reply(f"🌌 <b>{escape(empire.title)} Empire created!</b>\n🏰 Starting level: 1\n"
        "✨ Members can use /empirecontribute once per day.\n👑 Check progress with /empire.", parse_mode="HTML")

@router.message(Command("empirecontribute"))
async def empire_contribute_command(message: Message, db_session: AsyncSession = None, db_user=None, **kwargs):
    if message.chat.type not in ("group", "supergroup"):
        await message.reply("Use /empirecontribute inside your group."); return
    if not db_user or not message.from_user or db_session is None:
        await message.reply("Open Alexa in private chat with /start first, then try again."); return
    try:
        result = await contribute_to_empire(db_session, group_id=message.chat.id, user_id=db_user.id)
        await db_session.commit()
    except ValueError as exc:
        await db_session.rollback(); await message.reply(escape(str(exc))); return
    except Exception:
        await db_session.rollback(); await message.reply("⚠️ Contribution could not be saved. Please try again."); return
    if result["status"] == "already_contributed":
        await message.reply("✨ You have already contributed to this Empire today. Come back after the UTC reset."); return
    await message.reply(f"🌌 Empire contribution saved!\n+10 Empire XP · +5 Elite XP\n"
        f"🏰 Empire Level: {result['level']} · Progress: {result['xp']}/{result['level'] * 100} XP\nUse /vip to check your Elite status.")

@router.message(Command("vip", "pro"))
async def vip_profile(message: Message, db_session: AsyncSession = None, db_user=None, **kwargs):
    if not db_user or db_session is None:
        await message.reply("Open Alexa in private chat and press /start first, then use /vip."); return
    try:
        profile = await get_elite_profile(db_session, db_user.id)
        badges = await list_badges(db_session, db_user.id)
        await db_session.commit()
    except Exception:
        await db_session.rollback(); await message.reply("👑 Elite Club is temporarily unavailable."); return
    tier, icon = vip_tier(profile.xp or 0)
    next_target = next((n for n in (25, 75, 200, 500) if (profile.xp or 0) < n), None)
    progress = f"Next tier at {next_target} XP" if next_target else "🏆 Maximum tier reached!"
    badge_text = ", ".join(f"{BADGES.get(b.badge_code, ('🏅', b.badge_code))[0]} {BADGES.get(b.badge_code, ('🏅', b.badge_code))[1]}" for b in badges) or "None yet"
    await message.reply(f"👑 <b>ALEXA ELITE CLUB</b>\n\n{icon} Tier: <b>{tier}</b>\n✨ Elite XP: <b>{profile.xp}</b>\n"
        f"🏅 Collectible badges: <b>{len(badges)}</b>\n🎖 {escape(badge_text)}\n📈 {progress}\n\n"
        "Earn Elite XP from daily Empire contributions and opening your /vault. Elite status is earned through activity; no payment is required.",
        parse_mode="HTML")

@router.message(Command("vault"))
async def vault_command(message: Message, db_session: AsyncSession = None, db_user=None, **kwargs):
    if not db_user or db_session is None:
        await message.reply("Open Alexa in private chat and press /start first, then use /vault."); return
    try:
        result = await open_legendary_vault(db_session, user_id=db_user.id)
        await db_session.commit()
    except Exception:
        await db_session.rollback(); await message.reply("⚠️ The vault could not be opened safely. Please try again."); return
    if result["status"] == "already_opened":
        if result["reward_type"] == "badge":
            summary = f"you received the {BADGES.get(result['badge_code'], ('🏅', result['badge_code']))[1]} badge"
        else: summary = f"you received {result['amount']} coins"
        await message.reply(f"🎁 Today's Legendary Vault is already open — {summary}. Come back after the UTC reset."); return
    if result["reward_type"] == "badge":
        icon, name = BADGES[result["badge_code"]]
        detail = f"{icon} <b>{escape(name)} badge unlocked!</b>"
    else: detail = f"🪙 <b>+{result['amount']} coins</b> added to your wallet. Balance: {result['balance']} coins."
    await message.reply("🎁 <b>LEGENDARY MYSTERY VAULT</b>\n\n" + detail +
        "\n✨ +10 Elite XP\n\nCheck your status with /vip and collection with /badges.", parse_mode="HTML")

@router.message(Command("badges"))
async def badges_command(message: Message, db_session: AsyncSession = None, db_user=None, **kwargs):
    if not db_user or db_session is None:
        await message.reply("Open Alexa in private chat and press /start first, then use /badges."); return
    badges = await list_badges(db_session, db_user.id)
    if not badges:
        await message.reply("🏅 Your collection is empty. Open /vault for a chance to unlock a collectible badge."); return
    lines = ["🏅 <b>YOUR ELITE COLLECTION</b>", ""]
    for badge in badges:
        icon, name = BADGES.get(badge.badge_code, ("🏅", badge.badge_code))
        lines.append(f"{icon} <b>{escape(name)}</b>")
    await message.reply("\n".join(lines), parse_mode="HTML")
