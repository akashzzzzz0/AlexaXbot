from types import SimpleNamespace

from bot.handlers.group_promotion import (
    _ADMIN_PERMISSION_FIELDS,
    _admin_snapshot_key,
    _encode_admin_snapshot,
    _format_admin_snapshot,
)


def test_admin_snapshot_round_trips_all_telegram_permission_fields():
    values = {"status": "administrator"}
    for index, (_, attribute) in enumerate(_ADMIN_PERMISSION_FIELDS):
        values[attribute] = True if index % 3 == 0 else False if index % 3 == 1 else None

    snapshot = _encode_admin_snapshot(SimpleNamespace(**values), actor_id=123456)
    report = _format_admin_snapshot(snapshot)

    assert snapshot.startswith("v1|administrator|")
    assert report is not None
    assert "Status at capture: <b>Administrator</b>" in report
    assert "Changed by Telegram user: <code>123456</code>" in report
    assert "Manage chat: <b>YES</b>" in report
    assert "Change group info: <b>NO</b>" in report
    assert "Delete messages: <b>NOT EXPOSED</b>" in report


def test_admin_snapshot_key_is_scoped_to_the_exact_group():
    assert _admin_snapshot_key(-1001234567890) == "alexa_admin_snapshot:-1001234567890"


def test_malformed_admin_snapshot_is_ignored():
    assert _format_admin_snapshot("not-a-valid-snapshot") is None
    assert _format_admin_snapshot(None) is None



def test_private_invite_match_uses_live_active_group_admins_without_snapshot_rows():
    import asyncio

    from bot.handlers.group_promotion import _find_private_group_for_admin

    group = SimpleNamespace(
        id=-1001234567890,
        title="Private Test Group",
        username=None,
        type="supergroup",
        is_active=True,
        updated_at=None,
    )

    class FakeScalars:
        def all(self):
            return [group]

    class FakeSession:
        def __init__(self):
            self.added = []

        async def scalars(self, _query):
            return FakeScalars()

        async def get(self, _model, key):
            if key == "promo_admin_group:123":
                # A four-day-old add/admin event must not expire a valid
                # live membership match for a newly generated invite link.
                return SimpleNamespace(value=f"{group.id}:0")
            return None

        def add(self, value):
            self.added.append(value)

        async def flush(self):
            return None

    class FakeBot:
        async def get_me(self):
            return SimpleNamespace(id=999)

        async def get_chat_member(self, chat_id, user_id):
            assert chat_id == group.id
            assert user_id in {999, 123}
            return SimpleNamespace(status="administrator", can_delete_messages=True)

        async def get_chat(self, chat_id):
            assert chat_id == group.id
            return SimpleNamespace(
                id=chat_id,
                title="Private Test Group",
                username=None,
                type="supergroup",
            )

    session = FakeSession()
    matched_group, chat, error = asyncio.run(
        _find_private_group_for_admin(FakeBot(), session, user_id=123)
    )

    assert matched_group is group
    assert chat.title == "Private Test Group"
    assert error is None
    assert len(session.added) == 1
    assert session.added[0].key == _admin_snapshot_key(group.id)


def test_private_invite_match_rejects_multiple_live_admin_group_candidates():
    import asyncio

    from bot.handlers.group_promotion import _find_private_group_for_admin

    groups = [
        SimpleNamespace(
            id=-1001234567890 + index,
            title=f"Group {index}",
            username=None,
            type="supergroup",
            is_active=True,
            updated_at=None,
        )
        for index in (0, 1)
    ]

    class FakeScalars:
        def all(self):
            return groups

    class FakeSession:
        async def scalars(self, _query):
            return FakeScalars()

        async def get(self, _model, key):
            if key == "promo_admin_group:123":
                # A preferred old group must not bypass ambiguity checks.
                return SimpleNamespace(value=f"{groups[0].id}:0")
            return None

    class FakeBot:
        async def get_me(self):
            return SimpleNamespace(id=999)

        async def get_chat_member(self, chat_id, user_id):
            return SimpleNamespace(status="administrator")

        async def get_chat(self, chat_id):
            return SimpleNamespace(id=chat_id, title="Group", type="supergroup")

    group, chat, error = asyncio.run(
        _find_private_group_for_admin(FakeBot(), FakeSession(), user_id=123)
    )

    assert group is None
    assert chat is None
    assert "more than one active Alexa group" in error
