"""Schema compatibility regression test for the local SQLite fallback."""
import unittest
from sqlalchemy import create_engine, select, inspect
from sqlalchemy.exc import IntegrityError
from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session
from bot.database.models import Base, Group, SecuritySettings, User, CaptchaChallenge


class SQLiteSchemaTests(unittest.TestCase):
    def test_only_one_unsolved_captcha_per_group_member(self):
        engine = create_engine("sqlite:///:memory:")
        try:
            Base.metadata.create_all(engine)
            index_names = {item["name"] for item in inspect(engine).get_indexes("captcha_challenges")}
            self.assertIn("uq_captcha_one_unsolved_per_member_group", index_names)
            with Session(engine) as session:
                session.add(CaptchaChallenge(
                    group_id=-100123, user_id=456, challenge_type="math",
                    answer="4", expires_at=datetime.now(timezone.utc) + timedelta(minutes=1),
                ))
                session.commit()
                session.add(CaptchaChallenge(
                    group_id=-100123, user_id=456, challenge_type="math",
                    answer="5", expires_at=datetime.now(timezone.utc) + timedelta(minutes=1),
                ))
                with self.assertRaises(IntegrityError):
                    session.commit()
                session.rollback()
        finally:
            engine.dispose()

    def test_all_tables_create_and_basic_records_round_trip(self):
        engine = create_engine("sqlite:///:memory:")
        try:
            Base.metadata.create_all(engine)
            with Session(engine) as session:
                session.add(User(id=123456, first_name="Test User", telegram_id=123456))
                session.add(Group(id=-100123456, title="Test Group", type="supergroup"))
                session.flush()
                session.add(SecuritySettings(group_id=-100123456))
                session.commit()
                self.assertIsNotNone(session.get(User, 123456))
                self.assertIsNotNone(session.get(Group, -100123456))
                self.assertIsNotNone(session.scalar(select(SecuritySettings).where(SecuritySettings.group_id == -100123456)))
            self.assertGreaterEqual(len(Base.metadata.tables), 30)
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
