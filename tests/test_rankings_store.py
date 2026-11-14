import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from bot.services import rankings_store


def _message(group_id, user_id, name, text):
    return SimpleNamespace(
        chat=SimpleNamespace(id=group_id, type="supergroup"),
        from_user=SimpleNamespace(id=user_id, username=None, first_name=name, is_bot=False),
        text=text,
        caption=None,
        photo=None,
        document=None,
        sticker=None,
        video=None,
        audio=None,
        voice=None,
        animation=None,
        video_note=None,
        contact=None,
        location=None,
    )


class RankingsStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        rankings_store._DB_PATH = Path(self.temp_dir.name) / "rankings.sqlite3"
        rankings_store._READY = False

    def tearDown(self):
        self.temp_dir.cleanup()
        rankings_store._READY = False

    def test_rankings_are_isolated_by_group_and_period(self):
        async def scenario():
            await rankings_store.record_group_message(_message(-1001, 10, "Alpha", "hello"))
            await rankings_store.record_group_message(_message(-1001, 10, "Alpha", "again"))
            await rankings_store.record_group_message(_message(-1001, 20, "Beta", "hi"))
            await rankings_store.record_group_message(_message(-2002, 10, "Alpha", "other group"))

            today_rows, today_total = await rankings_store.get_rankings(-1001, "today")
            overall_rows, overall_total = await rankings_store.get_rankings(-1001, "overall")
            week_rows, week_total = await rankings_store.get_rankings(-1001, "week")
            other_rows, other_total = await rankings_store.get_rankings(-2002, "overall")

            self.assertEqual(today_total, 3)
            self.assertEqual(week_total, 3)
            self.assertEqual(overall_total, 3)
            self.assertEqual([row["user_id"] for row in today_rows], [10, 20])
            self.assertEqual(overall_rows[0]["message_count"], 2)
            self.assertEqual(other_total, 1)
            self.assertEqual(other_rows[0]["user_id"], 10)

            recap = await rankings_store.get_recap_messages(-1001, "today")
            self.assertEqual(len(recap), 3)
            self.assertEqual(recap[0][1], "hello")

        asyncio.run(scenario())

    def test_rankings_ignore_bot_and_slash_command_messages(self):
        async def scenario():
            bot_message = _message(-1001, 99, "Bot", "hello")
            bot_message.from_user.is_bot = True
            await rankings_store.record_group_message(bot_message)
            await rankings_store.record_group_message(_message(-1001, 10, "Alpha", "/rankings"))

            rows, total = await rankings_store.get_rankings(-1001, "overall")
            self.assertEqual(rows, [])
            self.assertEqual(total, 0)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
