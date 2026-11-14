import asyncio
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


def test_rankings_are_isolated_by_group_and_period(tmp_path):
    async def scenario():
        rankings_store._DB_PATH = tmp_path / "rankings.sqlite3"
        rankings_store._READY = False

        await rankings_store.record_group_message(_message(-1001, 10, "Alpha", "hello"))
        await rankings_store.record_group_message(_message(-1001, 10, "Alpha", "again"))
        await rankings_store.record_group_message(_message(-1001, 20, "Beta", "hi"))
        await rankings_store.record_group_message(_message(-2002, 10, "Alpha", "other group"))

        today_rows, today_total = await rankings_store.get_rankings(-1001, "today")
        overall_rows, overall_total = await rankings_store.get_rankings(-1001, "overall")
        week_rows, week_total = await rankings_store.get_rankings(-1001, "week")
        other_rows, other_total = await rankings_store.get_rankings(-2002, "overall")

        assert today_total == 3
        assert week_total == 3
        assert overall_total == 3
        assert [row["user_id"] for row in today_rows] == [10, 20]
        assert overall_rows[0]["message_count"] == 2
        assert other_total == 1
        assert other_rows[0]["user_id"] == 10

        recap = await rankings_store.get_recap_messages(-1001, "today")
        assert len(recap) == 3
        assert recap[0][1] == "hello"

    asyncio.run(scenario())


def test_rankings_ignore_bot_and_slash_command_messages(tmp_path):
    async def scenario():
        rankings_store._DB_PATH = tmp_path / "rankings-filter.sqlite3"
        rankings_store._READY = False

        bot_message = _message(-1001, 99, "Bot", "hello")
        bot_message.from_user.is_bot = True
        await rankings_store.record_group_message(bot_message)
        await rankings_store.record_group_message(_message(-1001, 10, "Alpha", "/rankings"))

        rows, total = await rankings_store.get_rankings(-1001, "overall")
        assert rows == []
        assert total == 0

    asyncio.run(scenario())
