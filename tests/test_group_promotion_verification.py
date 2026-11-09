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
