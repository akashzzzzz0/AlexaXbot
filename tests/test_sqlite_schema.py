"""Schema compatibility regression test for the local SQLite fallback."""
import unittest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from bot.database.models import Base, Group, SecuritySettings, User


class SQLiteSchemaTests(unittest.TestCase):
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
