"""Regression tests for atomic XP increments."""
import os
import tempfile
import unittest

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.database.models import Base, Economy, User
from bot.services.user_service import add_xp


class AtomicXpTests(unittest.IsolatedAsyncioTestCase):
    async def test_separate_rewards_accumulate_without_lost_updates(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with sessions() as session:
                session.add(User(id=987654321, telegram_id=987654321, first_name="XP Test"))
                await session.commit()

            async with sessions() as session:
                await add_xp(session, 987654321, 15)
                await session.commit()
            async with sessions() as session:
                await add_xp(session, 987654321, 25)
                await session.commit()

            async with sessions() as session:
                economy = await session.scalar(
                    select(Economy).where(Economy.user_id == 987654321)
                )
                self.assertIsNotNone(economy)
                self.assertEqual(economy.total_xp, 40)
        finally:
            await engine.dispose()
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
