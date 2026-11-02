"""Regression tests for wallet rewards, referrals, and single-use missions."""
import os
import tempfile
import unittest
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.database.models import (
    Base, User, Wallet, Transaction, Referral, Mission, UserMission,
)
from bot.services.economy_service import (
    add_coins, deduct_coins, process_referral,
)


class AtomicWalletAndClaimTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.path}")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with self.sessions() as session:
            session.add_all([
                User(id=987650001, telegram_id=987650001, first_name="Referrer"),
                User(id=987650002, telegram_id=987650002, first_name="Referred"),
            ])
            await session.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()
        os.unlink(self.path)

    async def test_wallet_rewards_and_deductions_accumulate(self):
        async with self.sessions() as session:
            await add_coins(session, 987650001, 100, "test_reward")
            await session.commit()
        async with self.sessions() as session:
            await add_coins(session, 987650001, 75, "test_reward")
            ok, balance = await deduct_coins(session, 987650001, 40, "test_spend")
            self.assertTrue(ok)
            self.assertEqual(balance, 135)
            await session.commit()
        async with self.sessions() as session:
            wallet = await session.scalar(
                select(Wallet).where(Wallet.user_id == 987650001)
            )
            self.assertEqual(wallet.balance, 135)
            self.assertEqual(wallet.total_earned, 175)
            self.assertEqual(wallet.total_spent, 40)
            transactions = (await session.scalars(
                select(Transaction).where(Transaction.wallet_id == wallet.id)
            )).all()
            self.assertEqual(len(transactions), 3)

    async def test_referral_reward_is_single_and_atomic(self):
        async with self.sessions() as session:
            first = await process_referral(session, 987650001, 987650002)
            duplicate = await process_referral(session, 987650001, 987650002)
            self.assertTrue(first)
            self.assertFalse(duplicate)
            await session.commit()
        async with self.sessions() as session:
            wallet = await session.scalar(
                select(Wallet).where(Wallet.user_id == 987650001)
            )
            referral = await session.scalar(
                select(Referral).where(Referral.referred_id == 987650002)
            )
            txs = (await session.scalars(
                select(Transaction).where(Transaction.wallet_id == wallet.id)
            )).all()
            self.assertEqual(wallet.balance, 0 + __import__("bot.config", fromlist=["settings"]).settings.REFERRAL_REWARD)
            self.assertEqual(wallet.total_earned, wallet.balance)
            self.assertTrue(referral.reward_given)
            self.assertEqual(len(txs), 1)

    async def test_mission_claim_transition_is_single_use(self):
        async with self.sessions() as session:
            mission = Mission(
                code="test_single_claim", type="daily", name_key="test",
                description_key="test", requirement_type="messages",
                requirement_value=1, reward_coins=50, reward_xp=0,
            )
            session.add(mission)
            await session.flush()
            row = UserMission(
                user_id=987650001, mission_id=mission.id, period="2026-11-02",
                progress=1, completed=True,
            )
            session.add(row)
            await session.flush()
            first = await session.execute(
                update(UserMission)
                .where(
                    UserMission.id == row.id,
                    UserMission.user_id == 987650001,
                    UserMission.completed.is_(True),
                    UserMission.completed_at.is_(None),
                )
                .values(completed_at=datetime.now(timezone.utc))
            )
            second = await session.execute(
                update(UserMission)
                .where(
                    UserMission.id == row.id,
                    UserMission.user_id == 987650001,
                    UserMission.completed.is_(True),
                    UserMission.completed_at.is_(None),
                )
                .values(completed_at=datetime.now(timezone.utc))
            )
            self.assertEqual(first.rowcount, 1)
            self.assertEqual(second.rowcount, 0)
            await session.commit()


if __name__ == "__main__":
    unittest.main()
