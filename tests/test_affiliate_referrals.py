"""Referral reward rules and affiliate request schema regression tests."""
import unittest

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from bot.database.models import AffiliateRequest, Base, User
from bot.handlers.affiliate import MIN_WITHDRAWAL, _earned_stars


class AffiliateRewardTierTests(unittest.TestCase):
    def test_reward_milestones_are_total_not_cumulative(self):
        self.assertEqual(_earned_stars(0), 0)
        self.assertEqual(_earned_stars(99), 0)
        self.assertEqual(_earned_stars(100), 50)
        self.assertEqual(_earned_stars(199), 50)
        self.assertEqual(_earned_stars(200), 100)
        self.assertEqual(_earned_stars(500), 100)

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
