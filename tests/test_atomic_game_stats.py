"""Regression tests for atomic game, duel, and Arena counters."""
import os
import tempfile
import unittest

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.database.models import Base, DuelStats, GameStats, ArenaProfile, Reputation, User
from bot.services.game_service import _increment_game_stats, _record_duel_result
from bot.services.arena_service import award_xp
from bot.services.user_service import add_reputation


class AtomicGameStatsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.path}")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with self.sessions() as session:
            session.add_all([
                User(id=987654321, telegram_id=987654321, first_name="Stats One"),
                User(id=987654322, telegram_id=987654322, first_name="Stats Two"),
            ])
            await session.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()
        os.unlink(self.path)

    async def test_game_counters_accumulate_across_sessions(self):
        for _ in range(3):
            async with self.sessions() as session:
                await _increment_game_stats(
                    session, 987654321, total_games=1, total_wins=1, dice_wins=1
                )
                await session.commit()
        async with self.sessions() as session:
            stats = await session.scalar(
                select(GameStats).where(GameStats.user_id == 987654321)
            )
            self.assertEqual(stats.total_games, 3)
            self.assertEqual(stats.total_wins, 3)
            self.assertEqual(stats.dice_wins, 3)

    async def test_duel_wins_and_losses_keep_streak_counters(self):
        for _ in range(2):
            async with self.sessions() as session:
                await _record_duel_result(session, 987654321, 987654322, 10)
                await session.commit()
        async with self.sessions() as session:
            winner = await session.scalar(
                select(DuelStats).where(DuelStats.user_id == 987654321)
            )
            loser = await session.scalar(
                select(DuelStats).where(DuelStats.user_id == 987654322)
            )
            self.assertEqual(winner.wins, 2)
            self.assertEqual(winner.win_streak, 2)
            self.assertEqual(winner.best_streak, 2)
            self.assertEqual(winner.coins_won, 20)
            self.assertEqual(loser.losses, 2)
            self.assertEqual(loser.win_streak, 0)
            self.assertEqual(loser.coins_lost, 20)

    async def test_reputation_counters_accumulate_across_sessions(self):
        async with self.sessions() as session:
            await add_reputation(session, 987654321, positive=2)
            await session.commit()
        async with self.sessions() as session:
            await add_reputation(session, 987654321, positive=3, negative=1)
            await session.commit()
        async with self.sessions() as session:
            reputation = await session.scalar(
                select(Reputation).where(Reputation.user_id == 987654321)
            )
            self.assertEqual(reputation.positive, 5)
            self.assertEqual(reputation.negative, 1)
            self.assertIsNotNone(reputation.last_given)

    async def test_arena_score_events_accumulate_and_are_idempotent(self):
        async with self.sessions() as session:
            first = await award_xp(session, 987654321, -100, "quiz", 20, "test-event-1", wins=1)
            duplicate = await award_xp(session, 987654321, -100, "quiz", 20, "test-event-1", wins=1)
            second = await award_xp(session, 987654321, -100, "quiz", 5, "test-event-2")
            await session.commit()
            profile = await session.get(ArenaProfile, 987654321)
            self.assertTrue(first)
            self.assertFalse(duplicate)
            self.assertTrue(second)
            self.assertEqual(profile.xp, 25)
            self.assertEqual(profile.total_games, 2)
            self.assertEqual(profile.wins, 1)


if __name__ == "__main__":
    unittest.main()
