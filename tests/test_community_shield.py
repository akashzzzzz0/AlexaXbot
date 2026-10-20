"""Regression tests for the isolated Community Shield module."""
import unittest

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.database.models import Base
from bot.services.community_shield_service import (
    COMMUNITY_FLAG_THRESHOLD,
    group_is_enabled,
    report_summary,
    report_user,
    set_group_enabled,
)


class CommunityShieldTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_reports_require_opt_in_and_distinct_groups(self):
        async with self.factory() as session:
            with self.assertRaises(ValueError):
                await report_user(
                    session, group_id=-1001, target_user_id=77,
                    reporter_user_id=1, reason="Suspicious scam offer",
                )
            await set_group_enabled(session, -1001, True)
            await set_group_enabled(session, -1002, True)
            await set_group_enabled(session, -1003, True)
            self.assertTrue(await group_is_enabled(session, -1001))
            for group_id in (-1001, -1002):
                self.assertTrue(await report_user(
                    session, group_id=group_id, target_user_id=77,
                    reporter_user_id=group_id * -1, reason="Evidence of scam offer",
                ))
            count, flagged = await report_summary(session, 77)
            self.assertEqual(count, 2)
            self.assertFalse(flagged)
            self.assertFalse(await report_user(
                session, group_id=-1001, target_user_id=77,
                reporter_user_id=99, reason="Another report from same group",
            ))
            self.assertTrue(await report_user(
                session, group_id=-1003, target_user_id=77,
                reporter_user_id=3, reason="Verified impersonation attempt",
            ))
            count, flagged = await report_summary(session, 77)
            self.assertEqual(count, COMMUNITY_FLAG_THRESHOLD)
            self.assertTrue(flagged)

    async def test_opted_out_group_reports_stop_counting(self):
        async with self.factory() as session:
            for group_id in (-2001, -2002, -2003):
                await set_group_enabled(session, group_id, True)
                await report_user(
                    session, group_id=group_id, target_user_id=88,
                    reporter_user_id=5, reason="Repeated scam solicitation",
                )
            await set_group_enabled(session, -2003, False)
            count, flagged = await report_summary(session, 88)
            self.assertEqual(count, 2)
            self.assertFalse(flagged)
