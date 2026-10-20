"""Regression tests for the admin-reviewed scam review queue."""
import unittest

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.database.models import Base
from bot.services.scam_review_service import (
    list_pending_reviews, resolve_scam_review, submit_scam_review,
)


class ScamReviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_submit_list_resolve_is_group_scoped(self):
        async with self.factory() as session:
            item = await submit_scam_review(
                session, group_id=-1001, target_user_id=22, reporter_user_id=11,
                reason="Impersonation and payment scam", evidence="Send money now",
            )
            await session.commit()
            self.assertEqual(len(await list_pending_reviews(session, -1001)), 1)
            self.assertEqual(len(await list_pending_reviews(session, -1002)), 0)
            self.assertFalse(await resolve_scam_review(
                session, review_id=item.id, group_id=-1002, resolver_user_id=33,
            ))
            self.assertTrue(await resolve_scam_review(
                session, review_id=item.id, group_id=-1001, resolver_user_id=33,
                note="Reviewed with group admin",
            ))
            await session.commit()
            self.assertEqual(len(await list_pending_reviews(session, -1001)), 0)

    async def test_reason_validation(self):
        async with self.factory() as session:
            with self.assertRaises(ValueError):
                await submit_scam_review(
                    session, group_id=-1001, target_user_id=22, reporter_user_id=11,
                    reason="scam", evidence="",
                )
