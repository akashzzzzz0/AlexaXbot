"""Regression tests for the isolated Ultra VIP package."""
import unittest
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from bot.database.models import Base, EliteBadge, EliteProfile, EmpireGuild, User, VaultClaim, Wallet
from bot.services.ultra_vip import contribute_to_empire, create_empire, open_legendary_vault, vip_tier

class UltraVipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self.now = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
        async with self.factory() as session:
            session.add(User(id=101, first_name="Tester", telegram_id=101))
            session.add(User(id=102, first_name="Second", telegram_id=102))
            await session.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_empire_progress_and_daily_contribution_limit(self):
        async with self.factory() as session:
            empire = await create_empire(session, group_id=-1001, title="Test Empire", admin_id=10)
            self.assertEqual(empire.level, 1)
            first = await contribute_to_empire(session, group_id=-1001, user_id=101, now=self.now)
            self.assertEqual(first["status"], "contributed")
            self.assertEqual(first["xp"], 10)
            again = await contribute_to_empire(session, group_id=-1001, user_id=101, now=self.now)
            self.assertEqual(again["status"], "already_contributed")
            other_member = await contribute_to_empire(session, group_id=-1001, user_id=102, now=self.now)
            self.assertEqual(other_member["status"], "contributed")
            profile = await session.get(EliteProfile, 101)
            self.assertEqual(profile.xp, 5)
            await session.commit()
            saved = await session.get(EmpireGuild, -1001)
            self.assertEqual(saved.xp, 20)

    async def test_vault_awards_once_per_day_and_persists_reward(self):
        async with self.factory() as session:
            first = await open_legendary_vault(session, user_id=101, now=self.now)
            self.assertEqual(first["status"], "opened")
            self.assertEqual(first["vip_xp"], 10)
            await session.commit()
            second = await open_legendary_vault(session, user_id=101, now=self.now)
            self.assertEqual(second["status"], "already_opened")
            self.assertEqual(second["reward_type"], first["reward_type"])
            self.assertEqual(second["amount"], first["amount"])
            claims = (await session.execute(select(VaultClaim).where(VaultClaim.user_id == 101))).scalars().all()
            self.assertEqual(len(claims), 1)
            profile = await session.get(EliteProfile, 101)
            self.assertEqual(profile.xp, 10)
            if first["reward_type"] == "coins":
                wallet = await session.scalar(select(Wallet).where(Wallet.user_id == 101))
                self.assertEqual(wallet.balance, first["amount"])
                self.assertIn(first["amount"], (50, 100, 150, 250, 500))
            else:
                badge = await session.scalar(select(EliteBadge).where(EliteBadge.user_id == 101))
                self.assertIsNotNone(badge)

    async def test_vip_tier_thresholds(self):
        self.assertEqual(vip_tier(0)[0], "ROOKIE")
        self.assertEqual(vip_tier(25)[0], "SILVER")
        self.assertEqual(vip_tier(75)[0], "GOLD")
        self.assertEqual(vip_tier(200)[0], "PLATINUM")
        self.assertEqual(vip_tier(500)[0], "DIAMOND")

if __name__ == "__main__":
    unittest.main()
