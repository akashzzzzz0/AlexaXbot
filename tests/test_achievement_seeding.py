"""Tests for idempotent, transaction-safe achievement seeding."""
import os
import tempfile
import unittest

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.database.models import Base, Achievement, User
from bot.services.achievement_service import ACHIEVEMENTS, seed_achievements


class AchievementSeedTests(unittest.IsolatedAsyncioTestCase):
    async def test_seed_is_idempotent_and_does_not_commit_callers_work(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

            async with sessions() as session:
                session.add(User(id=987659999, telegram_id=987659999, first_name="Pending"))
                await seed_achievements(session)
                await seed_achievements(session)
                count = await session.scalar(select(func.count(Achievement.id)))
                self.assertEqual(count, len(ACHIEVEMENTS))
                await session.rollback()

            async with sessions() as session:
                pending_user = await session.get(User, 987659999)
                achievement_count = await session.scalar(
                    select(func.count(Achievement.id))
                )
                self.assertIsNone(pending_user)
                self.assertEqual(achievement_count, 0)
        finally:
            await engine.dispose()
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
