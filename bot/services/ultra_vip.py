"""Targeted services for ALEXA Ultra VIP, Empire progression and Legendary Vault."""
from datetime import datetime, timezone
import random
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from bot.database.models import EliteBadge, EliteProfile, EmpireContribution, EmpireGuild, VaultClaim, Wallet

BADGES = {
    "rookie": ("🌟", "Rookie Star"),
    "royal": ("👑", "Royal Crown"),
    "cosmic": ("🌌", "Cosmic Legend"),
    "legendary": ("💎", "Legendary Gem"),
}

def _day_key(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date().isoformat()

def vip_tier(xp: int) -> tuple[str, str]:
    if xp >= 500: return "DIAMOND", "💎"
    if xp >= 200: return "PLATINUM", "💠"
    if xp >= 75: return "GOLD", "🥇"
    if xp >= 25: return "SILVER", "🥈"
    return "ROOKIE", "🌟"

async def get_elite_profile(session: AsyncSession, user_id: int) -> EliteProfile:
    profile = await session.get(EliteProfile, user_id)
    if profile is None:
        profile = EliteProfile(user_id=user_id, xp=0)
        session.add(profile)
        await session.flush()
    return profile

async def add_elite_xp(session: AsyncSession, user_id: int, amount: int) -> EliteProfile:
    profile = await get_elite_profile(session, user_id)
    profile.xp = max(0, (profile.xp or 0) + amount)
    await session.flush()
    return profile

async def create_empire(session: AsyncSession, *, group_id: int, title: str, admin_id: int) -> EmpireGuild:
    empire = await session.get(EmpireGuild, group_id)
    if empire is None:
        empire = EmpireGuild(group_id=group_id, title=(title or "Telegram Group")[:256],
                             level=1, xp=0, registered_by=admin_id, is_active=True)
        session.add(empire)
    else:
        empire.title = (title or "Telegram Group")[:256]
        empire.registered_by = admin_id
        empire.is_active = True
    await session.flush()
    return empire

async def contribute_to_empire(session: AsyncSession, *, group_id: int, user_id: int,
                               now: datetime | None = None) -> dict:
    today = _day_key(now)
    empire = await session.get(EmpireGuild, group_id)
    if not empire or not empire.is_active:
        raise ValueError("This group has no active Empire yet. An admin should use /empirecreate.")
    existing = await session.scalar(select(EmpireContribution.id).where(
        EmpireContribution.group_id == group_id, EmpireContribution.user_id == user_id,
        EmpireContribution.day_key == today))
    if existing is not None:
        return {"status": "already_contributed", "level": empire.level, "xp": empire.xp}
    session.add(EmpireContribution(group_id=group_id, user_id=user_id, day_key=today))
    await session.flush()
    empire.xp = (empire.xp or 0) + 10
    while empire.xp >= empire.level * 100:
        empire.xp -= empire.level * 100
        empire.level += 1
    profile = await add_elite_xp(session, user_id, 5)
    await session.flush()
    return {"status": "contributed", "level": empire.level, "xp": empire.xp, "vip_xp": profile.xp}

async def empire_ranking(session: AsyncSession, limit: int = 10):
    return (await session.execute(select(EmpireGuild).where(EmpireGuild.is_active.is_(True))
        .order_by(EmpireGuild.level.desc(), EmpireGuild.xp.desc(), EmpireGuild.title.asc())
        .limit(max(1, min(limit, 20))))).scalars().all()

async def list_badges(session: AsyncSession, user_id: int):
    return (await session.execute(select(EliteBadge).where(EliteBadge.user_id == user_id)
        .order_by(EliteBadge.earned_at.asc()))).scalars().all()

async def open_legendary_vault(session: AsyncSession, *, user_id: int,
                               now: datetime | None = None) -> dict:
    today = _day_key(now)
    previous = await session.scalar(select(VaultClaim).where(
        VaultClaim.user_id == user_id, VaultClaim.day_key == today))
    if previous is not None:
        return {"status": "already_opened", "reward_type": previous.reward_type,
                "amount": previous.reward_amount, "badge_code": previous.badge_code}
    reward_type = random.choices(("coins", "badge"), weights=(85, 15), k=1)[0]
    amount = random.choice((50, 100, 150, 250, 500)) if reward_type == "coins" else 0
    badge_code = random.choice(tuple(BADGES)) if reward_type == "badge" else None
    # The unique claim is flushed before balances change; conflicts roll back the whole transaction.
    session.add(VaultClaim(user_id=user_id, day_key=today, reward_type=reward_type,
                           reward_amount=amount, badge_code=badge_code))
    await session.flush()
    profile = await add_elite_xp(session, user_id, 10)
    balance = None
    if reward_type == "coins":
        wallet = await session.scalar(select(Wallet).where(Wallet.user_id == user_id))
        if wallet is None:
            wallet = Wallet(user_id=user_id, balance=0, bank_balance=0, total_earned=0, total_spent=0)
            session.add(wallet)
            await session.flush()
        wallet.balance = (wallet.balance or 0) + amount
        wallet.total_earned = (wallet.total_earned or 0) + amount
        balance = wallet.balance
    else:
        exists = await session.scalar(select(EliteBadge.id).where(
            EliteBadge.user_id == user_id, EliteBadge.badge_code == badge_code))
        if exists is None:
            session.add(EliteBadge(user_id=user_id, badge_code=badge_code))
    await session.flush()
    return {"status": "opened", "reward_type": reward_type, "amount": amount,
            "badge_code": badge_code, "balance": balance, "vip_xp": profile.xp}
