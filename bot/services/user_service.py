from datetime import datetime, timezone
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, update
from sqlalchemy.exc import IntegrityError
from bot.database.models import User, Wallet, Economy, Reputation, DuelStats, GameStats
import structlog

logger = structlog.get_logger()


async def get_or_create_wallet(session: AsyncSession, user_id: int) -> Wallet:
    wallet = await session.scalar(select(Wallet).where(Wallet.user_id == user_id))
    if not wallet:
        candidate = Wallet(user_id=user_id, balance=0, bank_balance=0)
        try:
            async with session.begin_nested():
                session.add(candidate)
                await session.flush()
            wallet = candidate
        except IntegrityError:
            wallet = await session.scalar(select(Wallet).where(Wallet.user_id == user_id))
            if wallet is None:
                raise
    return wallet


async def get_or_create_economy(session: AsyncSession, user_id: int) -> Economy:
    eco = await session.scalar(select(Economy).where(Economy.user_id == user_id))
    if not eco:
        candidate = Economy(user_id=user_id, xp=0, level=1)
        try:
            async with session.begin_nested():
                session.add(candidate)
                await session.flush()
            eco = candidate
        except IntegrityError:
            eco = await session.scalar(select(Economy).where(Economy.user_id == user_id))
            if eco is None:
                raise
    return eco


async def get_or_create_reputation(session: AsyncSession, user_id: int) -> Reputation:
    rep = await session.scalar(select(Reputation).where(Reputation.user_id == user_id))
    if not rep:
        candidate = Reputation(user_id=user_id, positive=0, negative=0)
        try:
            async with session.begin_nested():
                session.add(candidate)
                await session.flush()
            rep = candidate
        except IntegrityError:
            rep = await session.scalar(select(Reputation).where(Reputation.user_id == user_id))
            if rep is None:
                raise
    return rep


async def get_or_create_duel_stats(session: AsyncSession, user_id: int) -> DuelStats:
    stats = await session.scalar(select(DuelStats).where(DuelStats.user_id == user_id))
    if not stats:
        candidate = DuelStats(user_id=user_id)
        try:
            async with session.begin_nested():
                session.add(candidate)
                await session.flush()
            stats = candidate
        except IntegrityError:
            stats = await session.scalar(select(DuelStats).where(DuelStats.user_id == user_id))
            if stats is None:
                raise
    return stats


async def get_or_create_game_stats(session: AsyncSession, user_id: int) -> GameStats:
    stats = await session.scalar(select(GameStats).where(GameStats.user_id == user_id))
    if not stats:
        candidate = GameStats(user_id=user_id)
        try:
            async with session.begin_nested():
                session.add(candidate)
                await session.flush()
            stats = candidate
        except IntegrityError:
            stats = await session.scalar(select(GameStats).where(GameStats.user_id == user_id))
            if stats is None:
                raise
    return stats


async def set_user_language(session: AsyncSession, user_id: int, lang: str) -> None:
    await session.execute(
        update(User).where(User.id == user_id).values(language=lang)
    )


async def add_xp(session: AsyncSession, user_id: int, amount: int) -> tuple[int, int, bool]:
    from bot.utils.helpers import calculate_level
    eco = await get_or_create_economy(session, user_id)
    if amount == 0:
        new_level, current_xp, _ = calculate_level(eco.total_xp or 0)
        return new_level, current_xp, False

    # SQL-side increment prevents two simultaneous message/game rewards from
    # overwriting each other's XP with stale ORM values.
    result = await session.execute(
        update(Economy)
        .where(Economy.user_id == user_id)
        .values(total_xp=func.coalesce(Economy.total_xp, 0) + amount)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise RuntimeError(f"Economy row missing while adding XP for user {user_id}")
    await session.refresh(eco, attribute_names=["total_xp"])

    total_xp = eco.total_xp or 0
    previous_level, _, _ = calculate_level(total_xp - amount)
    new_level, current_xp, _ = calculate_level(total_xp)
    eco.level = new_level
    eco.xp = current_xp
    return new_level, current_xp, new_level > previous_level


async def get_top_users_by_level(session: AsyncSession, limit: int = 10) -> list:
    result = await session.execute(
        select(User, Economy)
        .join(Economy, Economy.user_id == User.id)
        .order_by(Economy.total_xp.desc())
        .limit(limit)
    )
    return result.all()


async def get_top_users_by_coins(session: AsyncSession, limit: int = 10) -> list:
    result = await session.execute(
        select(User, Wallet)
        .join(Wallet, Wallet.user_id == User.id)
        .order_by((Wallet.balance + Wallet.bank_balance).desc())
        .limit(limit)
    )
    return result.all()


async def get_top_reputation(session: AsyncSession, limit: int = 10) -> list:
    result = await session.execute(
        select(User, Reputation)
        .join(Reputation, Reputation.user_id == User.id)
        .order_by(Reputation.positive.desc())
        .limit(limit)
    )
    return result.all()
