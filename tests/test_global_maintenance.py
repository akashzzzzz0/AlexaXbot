import unittest
from types import SimpleNamespace

from bot.middlewares.global_maintenance import (
    _update_has_group_chat,
    _update_is_owner_maintenance_control,
)


def _chat_update(chat_type):
    return SimpleNamespace(
        message=SimpleNamespace(chat=SimpleNamespace(type=chat_type)),
        callback_query=None,
    )


class GlobalMaintenanceMiddlewareTests(unittest.TestCase):
    def test_detects_group_messages(self):
        self.assertTrue(_update_has_group_chat(_chat_update("group")))
        self.assertTrue(_update_has_group_chat(_chat_update("supergroup")))

    def test_does_not_classify_private_messages_as_group_updates(self):
        self.assertFalse(_update_has_group_chat(_chat_update("private")))

    def test_detects_group_callback_queries(self):
        update = SimpleNamespace(
            message=None,
            callback_query=SimpleNamespace(
                message=SimpleNamespace(chat=SimpleNamespace(type="supergroup"))
            ),
        )
        self.assertTrue(_update_has_group_chat(update))

    def test_ignores_updates_without_group_chat(self):
        self.assertFalse(
            _update_has_group_chat(
                SimpleNamespace(message=None, callback_query=None)
            )
        )

    def test_owner_can_open_private_owner_panel_during_maintenance(self):
        update = SimpleNamespace(
            message=SimpleNamespace(
                text="/owner",
                chat=SimpleNamespace(type="private"),
                from_user=SimpleNamespace(id=42),
            ),
            callback_query=None,
        )
        self.assertTrue(_update_is_owner_maintenance_control(update, 42))

    def test_non_owner_private_ai_chat_is_blocked_during_maintenance(self):
        update = SimpleNamespace(
            message=SimpleNamespace(
                text="Alexa hello",
                chat=SimpleNamespace(type="private"),
                from_user=SimpleNamespace(id=7),
            ),
            callback_query=None,
        )
        self.assertFalse(_update_is_owner_maintenance_control(update, 42))

    def test_owner_private_ai_chat_is_blocked_during_maintenance(self):
        update = SimpleNamespace(
            message=SimpleNamespace(
                text="Alexa hello",
                chat=SimpleNamespace(type="private"),
                from_user=SimpleNamespace(id=42),
            ),
            callback_query=None,
        )
        self.assertFalse(_update_is_owner_maintenance_control(update, 42))

    def test_owner_can_turn_maintenance_off_from_private_panel(self):
        update = SimpleNamespace(
            message=None,
            callback_query=SimpleNamespace(
                data="owner:maintenance:off",
                from_user=SimpleNamespace(id=42),
                message=SimpleNamespace(chat=SimpleNamespace(type="private")),
            ),
        )
        self.assertTrue(_update_is_owner_maintenance_control(update, 42))

    def test_owner_cannot_use_other_controls_during_maintenance(self):
        update = SimpleNamespace(
            message=None,
            callback_query=SimpleNamespace(
                data="owner:broadcast",
                from_user=SimpleNamespace(id=42),
                message=SimpleNamespace(chat=SimpleNamespace(type="private")),
            ),
        )
        self.assertFalse(_update_is_owner_maintenance_control(update, 42))

    def test_owner_cannot_manage_maintenance_from_a_group(self):
        update = SimpleNamespace(
            message=None,
            callback_query=SimpleNamespace(
                data="owner:maintenance:off",
                from_user=SimpleNamespace(id=42),
                message=SimpleNamespace(chat=SimpleNamespace(type="supergroup")),
            ),
        )
        self.assertFalse(_update_is_owner_maintenance_control(update, 42))


if __name__ == "__main__":
    unittest.main()
