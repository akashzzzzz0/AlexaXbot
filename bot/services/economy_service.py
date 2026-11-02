from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, or_, func
from sqlalchemy.exc import IntegrityError
from bot.database.models import Wallet, Transaction, Referral
from bot.services.user_service import get_or_create_wallet
from bot.config import settings
import structlog

logger = structlog.get_logger()


async def get_balance(session: AsyncSession, user_id: int) -> Tuple[int, int]:
    wallet = await get_or_create_wallet(session, user_id)
    return wallet.balance or 0, wallet.bank_balance or 0


async def add_coins(
    session: AsyncSession,
    user_id: int,
    amount: int,
    tx_type: str = "reward",
    description: str = "",
    reference_id: Optional[str] = None,
) -> int:
    wallet = await get_or_create_wallet(session, user_id)
    if amount <= 0:
        return wallet.balance or 0
    await session.execute(
        update(Wallet)
        .where(Wallet.id == wallet.id)
        .values(
            balance=func.coalesce(Wallet.balance, 0) + amount,
            total_earned=func.coalesce(Wallet.total_earned, 0) + amount,
        )
        .execution_options(synchronize_session=False)
    )
    await session.refresh(wallet)
    session.add(Transaction(
        wallet_id=wallet.id,
        amount=amount,
        type=tx_type,
        description=description,
        reference_id=reference_id,
    ))
    return wallet.balance or 0


async def deduct_coins(
    session: AsyncSession,
    user_id: int,
    amount: int,
    tx_type: str = "spend",
    description: str = "",
) -> Tuple[bool, int]:
    wallet = await get_or_create_wallet(session, user_id)
    if amount <= 0:
        return False, wallet.balance or 0
    result = await session.execute(
        update(Wallet)
        .where(Wallet.id == wallet.id, func.coalesce(Wallet.balance, 0) >= amount)
        .values(
            balance=Wallet.balance - amount,
            total_spent=func.coalesce(Wallet.total_spent, 0) + amount,
        )
        .execution_options(synchronize_session=False)
    )
    await session.refresh(wallet)
    if not result.rowcount:
        return False, wallet.balance or 0
    session.add(Transaction(
        wallet_id=wallet.id,
        amount=-amount,
        type=tx_type,
        description=description,
    ))
    return True, wallet.balance or 0


async def deposit_to_bank(session: AsyncSession, user_id: int, amount: int) -> Tuple[bool, int, int]:
    wallet = await get_or_create_wallet(session, user_id)
    if amount <= 0:
        return False, wallet.balance or 0, wallet.bank_balance or 0
    result = await session.execute(
        update(Wallet)
        .where(Wallet.id == wallet.id, func.coalesce(Wallet.balance, 0) >= amount)
        .values(
            balance=func.coalesce(Wallet.balance, 0) - amount,
            bank_balance=func.coalesce(Wallet.bank_balance, 0) + amount,
        )
        .execution_options(synchronize_session=False)
    )
    await session.refresh(wallet)
    if not result.rowcount:
        return False, wallet.balance or 0, wallet.bank_balance or 0
    return True, wallet.balance or 0, wallet.bank_balance or 0


async def withdraw_from_bank(session: AsyncSession, user_id: int, amount: int) -> Tuple[bool, int, int]:
    wallet = await get_or_create_wallet(session, user_id)
    if amount <= 0:
        return False, wallet.balance or 0, wallet.bank_balance or 0
    result = await session.execute(
        update(Wallet)
        .where(Wallet.id == wallet.id, func.coalesce(Wallet.bank_balance, 0) >= amount)
        .values(
            bank_balance=Wallet.bank_balance - amount,
            balance=func.coalesce(Wallet.balance, 0) + amount,
        )
        .execution_options(synchronize_session=False)
    )
    await session.refresh(wallet)
    if not result.rowcount:
        return False, wallet.balance or 0, wallet.bank_balance or 0
    return True, wallet.balance or 0, wallet.bank_balance or 0


async def transfer_coins(
    session: AsyncSession,
    from_user_id: int,
    to_user_id: int,
    amount: int,
) -> Tuple[bool, int, int]:
    """Returns (success, sender_balance, recipient_balance)."""
    if amount <= 0 or from_user_id == to_user_id:
        wallet = await get_or_create_wallet(session, from_user_id)
        return False, wallet.balance or 0, 0
    success, remaining = await deduct_coins(session, from_user_id, amount, "transfer_out", f"Transfer to {to_user_id}")
    if not success:
        return False, remaining, 0
    recipient_balance = await add_coins(session, to_user_id, amount, "transfer_in", f"Transfer from {from_user_id}")
    return True, remaining, recipient_balance


async def _claim_periodic_reward(
    session: AsyncSession,
    user_id: int,
    *,
    field: str,
    cooldown: timedelta,
    amount: int,
    tx_type: str,
    description: str,
) -> Tuple[bool, int, Optional[datetime]]:
    """Atomically claim a cooldown reward so concurrent updates cannot double-pay."""
    wallet = await get_or_create_wallet(session, user_id)
    now = datetime.now(timezone.utc)
    last_column = getattr(Wallet, field)
    result = await session.execute(
        update(Wallet)
        .where(
            Wallet.id == wallet.id,
            or_(last_column.is_(None), last_column <= now - cooldown),
        )
        .values(
            **{
                field: now,
                "balance": func.coalesce(Wallet.balance, 0) + amount,
                "total_earned": func.coalesce(Wallet.total_earned, 0) + amount,
            }
        )
        .execution_options(synchronize_session=False)
    )
    await session.refresh(wallet)
    if not result.rowcount:
        last = getattr(wallet, field)
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        next_claim = (last + cooldown) if last is not None else now + cooldown
        return False, 0, next_claim
    session.add(Transaction(wallet_id=wallet.id, amount=amount, type=tx_type, description=description))
    return True, amount, None


async def claim_daily_reward(session: AsyncSession, user_id: int) -> Tuple[bool, int, Optional[datetime]]:
    return await _claim_periodic_reward(
        session, user_id, field="last_daily", cooldown=timedelta(hours=24),
        amount=settings.DAILY_REWARD, tx_type="daily_reward", description="Daily reward",
    )


async def claim_weekly_reward(session: AsyncSession, user_id: int) -> Tuple[bool, int, Optional[datetime]]:
    return await _claim_periodic_reward(
        session, user_id, field="last_weekly", cooldown=timedelta(days=7),
        amount=settings.WEEKLY_REWARD, tx_type="weekly_reward", description="Weekly reward",
    )


async def claim_monthly_reward(session: AsyncSession, user_id: int) -> Tuple[bool, int, Optional[datetime]]:
    return await _claim_periodic_reward(
        session, user_id, field="last_monthly", cooldown=timedelta(days=30),
        amount=settings.MONTHLY_REWARD, tx_type="monthly_reward", description="Monthly reward",
    )


async def get_transactions(session: AsyncSession, user_id: int, limit: int = 10) -> list:
    wallet = await get_or_create_wallet(session, user_id)
    result = await session.execute(
        select(Transaction)
        .where(Transaction.wallet_id == wallet.id)
        .order_by(Transaction.created_at.desc())
        .limit(limit)
    )
    return result.scalars().all()


async def process_referral(
    session: AsyncSession,
    referrer_id: int,
    new_user_id: int,
) -> bool:
    existing = await session.scalar(
        select(Referral).where(Referral.referred_id == new_user_id)
    )
    if existing:
        return False

    referral = Referral(
        referrer_id=referrer_id,
        referred_id=new_user_id,
        reward_given=False,
    )
    try:
        async with session.begin_nested():
            session.add(referral)
            await session.flush()
    except IntegrityError:
        # A concurrent update already claimed this referred user.
        return False

    referrer_wallet = await get_or_create_wallet(session, referrer_id)
    # Atomic increment: concurrent referral completions must not overwrite
    # another reward written to the same wallet.
    await session.execute(
        update(Wallet)
        .where(Wallet.id == referrer_wallet.id)
        .values(
            balance=func.coalesce(Wallet.balance, 0) + settings.REFERRAL_REWARD,
            total_earned=func.coalesce(Wallet.total_earned, 0) + settings.REFERRAL_REWARD,
        )
        .execution_options(synchronize_session=False)
    )

    tx = Transaction(
        wallet_id=referrer_wallet.id,
        amount=settings.REFERRAL_REWARD,
        type="referral_reward",
        description=f"Referral reward for inviting user {new_user_id}",
    )
    session.add(tx)
    referral.reward_given = True

    logger.info("Referral reward granted", referrer_id=referrer_id, new_user_id=new_user_id, reward=settings.REFERRAL_REWARD)
    return True
