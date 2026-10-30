"""Referral reward rules and affiliate request schema regression tests."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import Session
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.database.models import AffiliatePayment, AffiliateRequest, Base, Referral, User
from bot.handlers.affiliate import (
    MIN_WITHDRAWAL,
    PREMIUM_3_MONTHS_STARS,
    premium_pre_checkout,
    premium_successful_payment,
    _earned_stars,
    _tracked_count,
    _valid_count,
    _referral_link_users,
    owner_approve,
    owner_mark_paid,
    owner_referrals,
    owner_reject,
    owner_reply_start,
    owner_request_queue,
    owner_view_request,
)


class AffiliateRewardTierTests(unittest.IsolatedAsyncioTestCase):
    def test_reward_milestones_accumulate_without_a_lifetime_cap(self):
        self.assertEqual(_earned_stars(0), 0)
        self.assertEqual(_earned_stars(1), 1)
        self.assertEqual(_earned_stars(99), 99)
        self.assertEqual(_earned_stars(100), 100)
        self.assertEqual(_earned_stars(199), 199)
        self.assertEqual(_earned_stars(200), 200)
        self.assertEqual(_earned_stars(500), 500)
        self.assertEqual(_earned_stars(1999), 1999)
        self.assertEqual(_earned_stars(2000), 2000)
        self.assertEqual(_earned_stars(2500), 2500)

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

    async def test_owner_referral_list_only_includes_users_who_requested_a_link(self):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as session:
                session.add_all([
                    User(id=200001, first_name="ReferralLinkUser", dm_started=True, referral_link_requested=True),
                    User(id=200002, first_name="NormalStartUser", dm_started=True, referral_link_requested=False),
                    User(id=200003, first_name="NotStartedUser", dm_started=False, referral_link_requested=True),
                    User(id=200004, first_name="BotLinkUser", is_bot=True, dm_started=True, referral_link_requested=True),
                ])
                await session.flush()
                users = await _referral_link_users(session)
                self.assertEqual([user.id for user in users], [200001])
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


class AffiliateStarsPaymentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _seed_order(self, session):
        user_id = 7123456
        session.add(User(id=user_id, first_name="Premium Test", telegram_id=user_id, dm_started=True))
        order = AffiliatePayment(
            user_id=user_id,
            invoice_payload=f"premium3:{user_id}:testnonce",
            amount=PREMIUM_3_MONTHS_STARS,
            currency="XTR",
            status="pending",
        )
        session.add(order)
        await session.flush()
        return user_id, order

    async def test_premium_price_and_payment_ledger_schema(self):
        self.assertEqual(PREMIUM_3_MONTHS_STARS, 1000)
        async with self.factory() as session:
            user_id, order = await self._seed_order(session)
            self.assertEqual(order.status, "pending")
            self.assertEqual(order.amount, 1000)
            self.assertIsNone(order.telegram_payment_charge_id)
            self.assertIsNone(await session.scalar(select(AffiliateRequest.id).where(
                AffiliateRequest.user_id == user_id,
                AffiliateRequest.request_type == "premium",
            )))

    async def test_precheckout_rejects_wrong_amount(self):
        async with self.factory() as session:
            user_id, order = await self._seed_order(session)
            query = SimpleNamespace(
                invoice_payload=order.invoice_payload,
                from_user=SimpleNamespace(id=user_id),
                currency="XTR",
                total_amount=PREMIUM_3_MONTHS_STARS - 1,
                answer=AsyncMock(),
            )
            await premium_pre_checkout(query, db_session=session)
            query.answer.assert_awaited_once()
            self.assertIs(query.answer.await_args.kwargs["ok"], False)

    async def test_verified_successful_payment_creates_one_owner_request(self):
        async with self.factory() as session:
            user_id, order = await self._seed_order(session)
            message = SimpleNamespace(
                successful_payment=SimpleNamespace(
                    invoice_payload=order.invoice_payload,
                    currency="XTR",
                    total_amount=PREMIUM_3_MONTHS_STARS,
                    telegram_payment_charge_id="charge-premium-test-001",
                ),
                from_user=SimpleNamespace(id=user_id),
                bot=SimpleNamespace(send_message=AsyncMock()),
                answer=AsyncMock(),
            )
            await premium_successful_payment(message, db_session=session)
            requests = (await session.scalars(select(AffiliateRequest).where(
                AffiliateRequest.user_id == user_id,
                AffiliateRequest.request_type == "premium",
            ))).all()
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0].status, "pending")
            self.assertEqual(requests[0].requested_stars, PREMIUM_3_MONTHS_STARS)
            self.assertEqual(order.status, "paid")
            self.assertEqual(order.telegram_payment_charge_id, "charge-premium-test-001")

            # Simulate Telegram redelivering the same successful-payment update.
            await premium_successful_payment(message, db_session=session)
            requests = (await session.scalars(select(AffiliateRequest).where(
                AffiliateRequest.user_id == user_id,
                AffiliateRequest.request_type == "premium",
            ))).all()
            self.assertEqual(len(requests), 1)

    async def test_unverified_payment_amount_does_not_create_request(self):
        async with self.factory() as session:
            user_id, order = await self._seed_order(session)
            message = SimpleNamespace(
                successful_payment=SimpleNamespace(
                    invoice_payload=order.invoice_payload,
                    currency="XTR",
                    total_amount=PREMIUM_3_MONTHS_STARS - 1,
                    telegram_payment_charge_id="charge-premium-invalid",
                ),
                from_user=SimpleNamespace(id=user_id),
                bot=SimpleNamespace(send_message=AsyncMock()),
                answer=AsyncMock(),
            )
            await premium_successful_payment(message, db_session=session)
            self.assertEqual(await session.scalar(select(AffiliateRequest.id).where(
                AffiliateRequest.user_id == user_id,
                AffiliateRequest.request_type == "premium",
            )), None)
            self.assertEqual(order.status, "pending")


class AffiliateOwnerPermissionTests(unittest.IsolatedAsyncioTestCase):
    def make_callback(self, data):
        message = SimpleNamespace(
            photo=None,
            edit_text=AsyncMock(),
            edit_caption=AsyncMock(),
            answer=AsyncMock(),
        )
        return SimpleNamespace(
            from_user=SimpleNamespace(id=987654321),
            data=data,
            message=message,
            answer=AsyncMock(),
            bot=SimpleNamespace(send_message=AsyncMock()),
        )

    async def assert_owner_only(self, handler, data, **extra):
        callback = self.make_callback(data)
        session = AsyncMock()
        state = AsyncMock()
        with patch("bot.handlers.affiliate._owner", return_value=False):
            if handler is owner_reply_start:
                await handler(callback, state=state, db_session=session)
            else:
                await handler(callback, db_session=session)
        callback.answer.assert_awaited_once()
        self.assertIn("Owner only", callback.answer.await_args.args[0])
        session.get.assert_not_awaited()
        callback.message.edit_text.assert_not_awaited()

    async def test_non_owner_cannot_open_referral_board_or_request_queue(self):
        await self.assert_owner_only(owner_referrals, "owner:referrals:page:0")
        await self.assert_owner_only(owner_request_queue, "owner:affiliate:requests:page:0")

    async def test_non_owner_cannot_view_or_approve_requests(self):
        await self.assert_owner_only(owner_view_request, "owner:affiliate:view:1")
        await self.assert_owner_only(owner_approve, "owner:affiliate:approve:1")

    async def test_non_owner_cannot_reject_or_mark_paid(self):
        await self.assert_owner_only(owner_reject, "owner:affiliate:reject:1")
        await self.assert_owner_only(owner_mark_paid, "owner:affiliate:paid:1")

    async def test_non_owner_cannot_start_private_reply_flow(self):
        await self.assert_owner_only(owner_reply_start, "owner:affiliate:reply:1")


if __name__ == "__main__":
    unittest.main()
