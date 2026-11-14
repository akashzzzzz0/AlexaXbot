import unittest
from types import SimpleNamespace

from bot.middlewares.global_maintenance import _update_has_group_chat


def _chat_update(chat_type):
    return SimpleNamespace(
        message=SimpleNamespace(chat=SimpleNamespace(type=chat_type)),
        callback_query=None,
    )


class GlobalMaintenanceMiddlewareTests(unittest.TestCase):
    def test_detects_group_messages(self):
        self.assertTrue(_update_has_group_chat(_chat_update("group")))
        self.assertTrue(_update_has_group_chat(_chat_update("supergroup")))

    def test_does_not_block_private_messages(self):
        self.assertFalse(_update_has_group_chat(_chat_update("private")))

    def test_detects_group_callback_queries(self):
        update = SimpleNamespace(
            message=None,
            callback_query=SimpleNamespace(
                message=SimpleNamespace(
                    chat=SimpleNamespace(type="supergroup")
                )
            ),
        )
        self.assertTrue(_update_has_group_chat(update))

    def test_ignores_updates_without_group_chat(self):
        self.assertFalse(_update_has_group_chat(SimpleNamespace(message=None, callback_query=None)))


if __name__ == "__main__":
    unittest.main()
