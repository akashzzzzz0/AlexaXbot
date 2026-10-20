"""Regression tests for Multiverse Wars scoring, group isolation, and challenge fairness."""
import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.database.models import Base, WarChallenge, WarSeasonScore
from bot.services.multiverse_wars import (
    accept_challenge, create_challenge, daily_mission, leaderboard,
    register_guild, season_key, settle_expired_challenges, submit_answer,
)


class MultiverseWarsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self.now = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
        async with self.factory() as session:
            await register_guild(session, group_id=-1001, title="Alpha Guild", username=None, admin_id=11)
            await register_guild(session, group_id=-1002, title="Beta Guild", username=None, admin_id=22)
            await session.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_daily_mission_awards_once_and_is_group_scoped(self):
        _, answer = daily_mission(self.now)
        async with self.factory() as session:
            result = await submit_answer(
                session, group_id=-1001, user_id=101, answer=answer, now=self.now,
            )
            self.assertEqual(result["status"], "correct")
            self.assertEqual(result["points"], 5)
            await session.commit()
            again = await submit_answer(
                session, group_id=-1001, user_id=101, answer=answer, now=self.now,
            )
            self.assertEqual(again["status"], "already_completed")
            other_group = await submit_answer(
                session, group_id=-1002, user_id=101, answer=answer, now=self.now,
            )
            self.assertEqual(other_group["status"], "correct")
            rows = await leaderboard(session, season_key(self.now))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0][1].points, 5)

    async def test_three_attempt_limit(self):
        _, answer = daily_mission(self.now)
        wrong = "not-the-answer" if answer != "not-the-answer" else "wrong"
        async with self.factory() as session:
            for used in range(1, 4):
                result = await submit_answer(
                    session, group_id=-1001, user_id=101, answer=wrong, now=self.now,
                )
                self.assertEqual(result["status"], "incorrect")
                self.assertEqual(result["attempts"], used)
            exhausted = await submit_answer(
                session, group_id=-1001, user_id=101, answer=answer, now=self.now,
            )
            self.assertEqual(exhausted["status"], "attempts_exhausted")

    async def test_challenge_acceptance_contribution_and_expiry_bonus(self):
        _, answer = daily_mission(self.now)
        async with self.factory() as session:
            challenge = await create_challenge(
                session, challenger_group_id=-1001, opponent_group_id=-1002,
                created_by=11, now=self.now,
            )
            await session.commit()
            with self.assertRaises(ValueError):
                await accept_challenge(
                    session, challenge_id=challenge.id, group_id=-1001,
                    admin_id=11, now=self.now,
                )
            accepted = await accept_challenge(
                session, challenge_id=challenge.id, group_id=-1002,
                admin_id=22, now=self.now,
            )
            self.assertEqual(accepted.status, "active")
            await session.commit()

            first = await submit_answer(
                session, group_id=-1001, user_id=101, answer=answer, now=self.now + timedelta(minutes=1),
            )
            second = await submit_answer(
                session, group_id=-1002, user_id=202, answer=answer, now=self.now + timedelta(minutes=1),
            )
            self.assertEqual(first["challenge_points"], 1)
            self.assertEqual(second["challenge_points"], 1)
            await session.commit()

            resolved_count = await settle_expired_challenges(session, self.now + timedelta(hours=25))
            self.assertEqual(resolved_count, 1)
            row = await session.get(WarChallenge, challenge.id)
            self.assertEqual(row.status, "completed")
            self.assertIsNone(row.winner_group_id)  # equal scores resolve as a draw
            left = await session.scalar(select(WarSeasonScore).where(
                WarSeasonScore.group_id == -1001,
                WarSeasonScore.season_key == season_key(self.now),
            ))
            self.assertEqual(left.draws, 1)
            await session.commit()

    async def test_cannot_challenge_unregistered_group_or_self(self):
        async with self.factory() as session:
            with self.assertRaises(ValueError):
                await create_challenge(
                    session, challenger_group_id=-1001, opponent_group_id=-1001,
                    created_by=11, now=self.now,
                )
            with self.assertRaises(ValueError):
                await create_challenge(
                    session, challenger_group_id=-1001, opponent_group_id=-9999,
                    created_by=11, now=self.now,
                )


if __name__ == "__main__":
    unittest.main()
