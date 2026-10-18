"""Referral reward rules and affiliate request schema regression tests."""
import unittest

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.database.models import AffiliateRequest, Base, Referral, User
from bot.handlers.affiliate import MIN_WITHDRAWAL, _earned_stars, _tracked_count, _valid_count


class AffiliateRewardTierTests(unittest.IsolatedAsyncioTestCase):
    def test_reward_milestones_are_total_not_cumulative(self):
        self.assertEqual(_earned_stars(0), 0)
        self.assertEqual(_earned_stars(99), 0)
        self.assertEqual(_earned_stars(100), 50)
        self.assertEqual(_earned_stars(199), 50)
        self.assertEqual(_earned_stars(200), 100)
        self.assertEqual(_earned_stars(500), 100)

    async def test_valid_referrals_exclude_accounts_that_have_not_started_dm_and_bots(self):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                session.add_all([
                    User(id=100001, first_name="Inviter", telegram_id=100001, dm_started=True, is_bot=False),
                    User(id=100002, first_name="Valid", telegram_id=100002, dm_started=True, is_bot=False),
                    User(id=100003, first_name="NotStarted", telegram_id=100003, dm_started=False, is_bot=False),
                    User(id=100004, first_name="BotAccount", telegram_id=100004, dm_started=True, is_bot=True),
                ])
                await session.flush()
                session.add_all([
                    Referral(referrer_id=100001, referred_id=100002),
                    Referral(referrer_id=100001, referred_id=100003),
                    Referral(referrer_id=100001, referred_id=100004),
                ])
                await session.flush()
                self.assertEqual(await _tracked_count(session, 100001), 3)
                self.assertEqual(await _valid_count(session, 100001), 1)
        finally:
            await engine.dispose()

    def test_minimum_withdrawal_is_100_stars(self):
        self.assertEqual(MIN_WITHDRAWAL, 100)

    def test_affiliate_request_table_is_created_and_links_to_user(self):
        engine = create_engine("sqlite:///:memory:")
        try:
            Base.metadata.create_all(engine)
            columns = {column["name"] for column in inspect(engine).get_columns("affiliate_requests")}
            self.assertTrue({"id", "user_id", "request_type", "requested_stars", "status", "admin_reply"} <= columns)
            with Session(engine) as session:
                session.add(User(id=123456, first_name="Referral Test", telegram_id=123456))
                session.flush()
                request = AffiliateRequest(
                    user_id=123456,
                    request_type="stars",
                    requested_stars=100,
                    status="pending",
                )
                session.add(request)
                session.commit()
                self.assertIsNotNone(session.get(AffiliateRequest, request.id))
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
