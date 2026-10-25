"""ALEXA legacy coin-game commands; all actions affect virtual bot coins only."""
import random
from datetime import datetime, timedelta, timezone
from html import escape
from aiogram import Router
from aiogram.types import Message
from aiogram.filters import Command
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession
from bot.config import settings
from bot.database.models import User, Wallet, Economy, GameProfile, UserShopItem, ShopItem
from bot.services.user_service import get_or_create_wallet, get_or_create_economy
from bot.services.economy_service import add_coins, deduct_coins
from bot.utils.helpers import parse_duration, format_number, format_duration

router = Router()

async def profile(session: AsyncSession, user_id: int) -> GameProfile:
    row = await session.get(GameProfile, user_id)
    if row is None:
        row = GameProfile(user_id=user_id)
        session.add(row)
        await session.flush()
    return row

async def target_from_reply(message: Message, session: AsyncSession):
    if not message.reply_to_message or not message.reply_to_message.from_user:
        return None
    tg = message.reply_to_message.from_user
    row = await session.get(User, tg.id)
    if row is None:
        row = User(id=tg.id, telegram_id=tg.id, username=tg.username, first_name=tg.first_name or "User", last_name=tg.last_name, is_bot=tg.is_bot)
        session.add(row)
        await session.flush()
    return row

@router.message(Command("bal"))
async def legacy_balance(message: Message, _: callable, db_session: AsyncSession, db_user, **kwargs):
    wallet = await get_or_create_wallet(db_session, db_user.id)
    await message.reply(f"👛 <b>Balance</b>\nPocket: {format_number(wallet.balance or 0)} coins\nBank: {format_number(wallet.bank_balance or 0)} coins", parse_mode="HTML")

@router.message(Command("protect"))
async def protect(message: Message, _: callable, db_session: AsyncSession, db_user, **kwargs):
    parts = (message.text or "").split()
    duration_seconds = parse_duration(parts[1]) if len(parts) > 1 else 86400
    if not duration_seconds or duration_seconds <= 0 or duration_seconds > 30 * 86400:
        await message.reply("Usage: /protect 1d (maximum 30 days).")
        return
    duration = timedelta(seconds=duration_seconds)
    days = max(1, (duration_seconds + 86399) // 86400)
    cost = settings.PROTECT_COST_PER_DAY * days
    ok, balance = await deduct_coins(db_session, db_user.id, cost, "protection", f"Protection for {days} day(s)")
    if not ok:
        await message.reply(f"Not enough coins. Need {cost}; your balance is {balance}.")
        return
    row = await profile(db_session, db_user.id)
    row.protection_until = datetime.now(timezone.utc) + duration
    await message.reply(f"🛡 Protection enabled for {format_duration(duration_seconds)}. Cost: {cost} coins (per started day).")

@router.message(Command("unprotect"))
async def unprotect(message: Message, db_session: AsyncSession, db_user, **kwargs):
    row = await profile(db_session, db_user.id)
    row.protection_until = None
    await message.reply("🛡 Protection disabled.")

@router.message(Command("kill"))
async def kill(message: Message, db_session: AsyncSession, db_user, **kwargs):
    target = await target_from_reply(message, db_session)
    if not target:
        await message.reply("Reply to a user's message with /kill. This is a virtual coin-game action only.")
        return
    if target.id == db_user.id or target.is_bot:
        await message.reply("Choose another human player.")
        return
    victim = await profile(db_session, target.id)
    now = datetime.now(timezone.utc)
    protected = victim.protection_until and (victim.protection_until.replace(tzinfo=timezone.utc) > now)
    if protected:
        await message.reply("🛡 That player is protected.")
        return
    killer = await profile(db_session, db_user.id)
    # A valid /kill action should register consistently; the previous 50% random
    # failure was confusing users and left the kill/death scores unchanged.
    killer.kills = (killer.kills or 0) + 1
    victim.deaths = (victim.deaths or 0) + 1
    killer.last_kill_at = now
    await message.reply(f"🎮 {escape(target.first_name)} was defeated in the virtual game. Kill score +1.")

@router.message(Command("rob"))
async def rob(message: Message, db_session: AsyncSession, db_user, **kwargs):
    target = await target_from_reply(message, db_session)
    if not target:
        await message.reply("Reply to a user's message with /rob. This only affects virtual coins.")
        return
    if target.id == db_user.id or target.is_bot:
        await message.reply("Choose another human player.")
        return
    victim_profile = await profile(db_session, target.id)
    now = datetime.now(timezone.utc)
    if victim_profile.protection_until and victim_profile.protection_until.replace(tzinfo=timezone.utc) > now:
        await message.reply("🛡 That player is protected.")
        return
    robber = await profile(db_session, db_user.id)
    if robber.last_rob_at and (now - robber.last_rob_at.replace(tzinfo=timezone.utc)).total_seconds() < 3600:
        await message.reply("Rob cooldown: try again in an hour.")
        return
    robber.last_rob_at = now
    # A valid rob attempt succeeds when the target is unprotected and has coins.
    # The one-hour cooldown limits repeated stealing.
    victim_wallet = await get_or_create_wallet(db_session, target.id)
    amount = min(max(0, victim_wallet.balance or 0), settings.MAX_ROB_AMOUNT, max(1, (victim_wallet.balance or 0) // 5))
    if amount <= 0:
        await message.reply("That player has no pocket coins to rob.")
        return
    ok, _ = await deduct_coins(db_session, target.id, amount, "robbed", f"Robbed by {db_user.id}")
    if ok:
        await add_coins(db_session, db_user.id, amount, "rob_success", f"Robbed user {target.id}")
        robber.successful_robs = (robber.successful_robs or 0) + 1
        await message.reply(f"💰 You stole {amount} virtual coins from {escape(target.first_name)}.")
    else:
        await message.reply("Rob failed; target balance changed. Try again.")

@router.message(Command("revive"))
async def revive(message: Message, db_session: AsyncSession, db_user, **kwargs):
    row = await profile(db_session, db_user.id)
    if (row.deaths or 0) <= 0:
        await message.reply("You have no game losses to revive from.")
        return
    ok, balance = await deduct_coins(db_session, db_user.id, settings.REVIVE_COST, "revive", "Revive game profile")
    if not ok:
        await message.reply(f"Revive costs {settings.REVIVE_COST} coins. Balance: {balance}.")
        return
    row.deaths = max(0, row.deaths - 1)
    row.revives = (row.revives or 0) + 1
    await message.reply("✨ Game profile revived. One virtual defeat removed.")

@router.message(Command("rank"))
async def rank(message: Message, db_session: AsyncSession, db_user, **kwargs):
    eco = await get_or_create_economy(db_session, db_user.id)
    wallet = await get_or_create_wallet(db_session, db_user.id)
    gp = await profile(db_session, db_user.id)
    await message.reply(f"🏅 <b>Your Rank</b>\nLevel: {eco.level or 1}\nXP: {eco.total_xp or 0}\nCoins: {wallet.balance or 0}\nVirtual kills: {gp.kills or 0}\nSuccessful robs: {gp.successful_robs or 0}\nDefeats: {gp.deaths or 0}", parse_mode="HTML")

@router.message(Command("leaders"))
async def leaders(message: Message, db_session: AsyncSession, **kwargs):
    rows = (await db_session.execute(select(User, Wallet).join(Wallet, Wallet.user_id == User.id).order_by(desc(Wallet.balance)).limit(10))).all()
    text = "🏆 <b>Richest Players</b>\n\n" + ("\n".join(f"{i}. {escape(u.first_name)} — {format_number(w.balance or 0)} coins" for i,(u,w) in enumerate(rows,1)) if rows else "No players yet.")
    await message.reply(text, parse_mode="HTML")

@router.message(Command("toprich"))
async def toprich(message: Message, db_session: AsyncSession, **kwargs):
    await leaders(message, db_session, **kwargs)

@router.message(Command("topkill"))
async def topkill(message: Message, db_session: AsyncSession, **kwargs):
    rows = (await db_session.execute(select(User, GameProfile).join(GameProfile, GameProfile.user_id == User.id).order_by(desc(GameProfile.kills)).limit(10))).all()
    text = "⚔️ <b>Top Virtual Kill Scores</b>\n\n" + ("\n".join(f"{i}. {escape(u.first_name)} — {g.kills or 0}" for i,(u,g) in enumerate(rows,1)) if rows else "No scores yet.")
    await message.reply(text, parse_mode="HTML")

@router.message(Command("toprob"))
async def toprob(message: Message, db_session: AsyncSession, **kwargs):
    rows = (await db_session.execute(select(User, GameProfile).join(GameProfile, GameProfile.user_id == User.id).order_by(desc(GameProfile.successful_robs)).limit(10))).all()
    text = "💰 <b>Top Successful Robs</b>\n\n" + ("\n".join(f"{i}. {escape(u.first_name)} — {g.successful_robs or 0}" for i,(u,g) in enumerate(rows,1)) if rows else "No scores yet.")
    await message.reply(text, parse_mode="HTML")

@router.message(Command("task"))
async def task(message: Message, _: callable, **kwargs):
    await message.reply("🎯 <b>Tasks</b>\n/daily — claim daily coins\n/weekly — claim weekly coins\n/monthly — claim monthly coins\n/games — open games menu\n/mission buttons show configured missions.", parse_mode="HTML")

@router.message(Command("items"))
async def items(message: Message, db_session: AsyncSession, db_user, **kwargs):
    rows = (await db_session.execute(select(UserShopItem, ShopItem).join(ShopItem, ShopItem.id == UserShopItem.item_id).where(UserShopItem.user_id == db_user.id).order_by(UserShopItem.purchased_at.desc()).limit(30))).all()
    text = "🎒 <b>Your Items</b>\n\n" + ("\n".join(f"{item.icon or '🎁'} {escape(item.name_key)} × {owned.quantity}" for owned,item in rows) if rows else "You have no items yet. Open /menu → Economy → Shop to buy items.")
    await message.reply(text, parse_mode="HTML")

@router.message(Command("pfp"))
async def pfp(message: Message, **kwargs):
    target = message.reply_to_message.from_user if message.reply_to_message and message.reply_to_message.from_user else message.from_user
    photos = await message.bot.get_user_profile_photos(target.id, limit=1)
    if not photos.total_count:
        await message.reply("No public profile photo found.")
        return
    await message.answer_photo(photos.photos[0][-1].file_id, caption=f"🖼 Profile photo: {escape(target.full_name or target.first_name)}")

@router.message(Command("bluff"))
async def bluff(message: Message, db_session: AsyncSession, db_user, **kwargs):
    wallet = await get_or_create_wallet(db_session, db_user.id)
    wager = 50
    if (wallet.balance or 0) < wager:
        await message.reply("Bluff game costs 50 coins to play.")
        return
    await deduct_coins(db_session, db_user.id, wager, "bluff_bet", "Bluff game wager")
    if random.random() < 0.45:
        reward = wager * 2
        await add_coins(db_session, db_user.id, reward, "bluff_win", "Bluff game win")
        await message.reply(f"🃏 Bluff successful! You won {reward} coins.")
    else:
        await message.reply("🃏 Your bluff was called. You lost 50 coins.")

@router.message(Command("hack"))
async def hack(message: Message, db_session: AsyncSession, db_user, **kwargs):
    wallet = await get_or_create_wallet(db_session, db_user.id)
    wager = 25
    if (wallet.balance or 0) < wager:
        await message.reply("Hack mini-game costs 25 coins to play.")
        return
    await deduct_coins(db_session, db_user.id, wager, "hack_bet", "Hack mini-game wager")
    if random.random() < 0.35:
        reward = 100
        await add_coins(db_session, db_user.id, reward, "hack_win", "Hack mini-game win")
        await message.reply(f"💻 Simulation solved! You won {reward} virtual coins.")
    else:
        await message.reply("💻 Simulation failed. You lost 25 virtual coins.")


@router.message(Command("bomb"))
async def bomb_game(message: Message, db_session: AsyncSession, db_user, **kwargs):
    """Simple virtual-coin bomb game."""
    wager = 20
    ok, balance = await deduct_coins(
        db_session, db_user.id, wager, "bomb_bet", "Virtual bomb game entry"
    )
    if not ok:
        await message.reply(f"💣 Bomb game costs {wager} coins. Your balance: {balance}.")
        return
    if random.random() < 0.55:
        reward = wager * 2
        await add_coins(db_session, db_user.id, reward, "bomb_win", "Virtual bomb game win")
        await message.reply(f"💣 You found the safe path! Reward: {reward} coins.")
    else:
        await message.reply(f"💥 Boom! You lost {wager} virtual coins. Try again later.")


@router.message(Command("card"))
async def card_game(message: Message, db_session: AsyncSession, db_user, **kwargs):
    """Draw a virtual card against the bot; no real-money value."""
    wager = 25
    ok, balance = await deduct_coins(
        db_session, db_user.id, wager, "card_bet", "Virtual card game entry"
    )
    if not ok:
        await message.reply(f"🃏 Card game costs {wager} coins. Your balance: {balance}.")
        return
    player_card = random.randint(1, 13)
    bot_card = random.randint(1, 13)
    if player_card > bot_card:
        reward = wager * 2
        await add_coins(db_session, db_user.id, reward, "card_win", "Virtual card game win")
        result = f"🏆 You win! Reward: {reward} coins."
    elif player_card == bot_card:
        await add_coins(db_session, db_user.id, wager, "card_draw", "Virtual card game refund")
        result = f"🤝 Draw — your {wager} coins were returned."
    else:
        result = f"❌ Bot wins. You lost {wager} virtual coins."
    ranks = {1: "A", 11: "J", 12: "Q", 13: "K"}
    player_label = ranks.get(player_card, str(player_card))
    bot_label = ranks.get(bot_card, str(bot_card))
    await message.reply(
        f"🃏 <b>Card Duel</b>\nYour card: <b>{player_label}</b> · Bot card: <b>{bot_label}</b>\n\n{result}",
        parse_mode="HTML",
    )
