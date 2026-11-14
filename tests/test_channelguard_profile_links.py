"""Regression tests for ChannelGuard's exposed profile-link detection."""
import unittest
from types import SimpleNamespace

from bot.handlers.welcome import _channelguard_profile_links


class FakeBot:
    def __init__(self, chats):
        self.chats = chats

    async def get_chat(self, chat_id):
        return self.chats[chat_id]


class ChannelGuardProfileTests(unittest.IsolatedAsyncioTestCase):
    async def test_connected_channel_without_description_link_is_not_a_violation(self):
        bot = FakeBot({
            100: SimpleNamespace(
                bio="",
                personal_chat=SimpleNamespace(id=-100, username="updates_channel"),
            ),
            -100: SimpleNamespace(description="Group updates and announcements."),
        })

        evidence, available = await _channelguard_profile_links(bot, 100)

        self.assertTrue(available)
        self.assertEqual(evidence, [])

    async def test_bio_link_is_detected(self):
        bot = FakeBot({
            101: SimpleNamespace(
                bio="More info: https://example.com",
                personal_chat=None,
            ),
        })

        evidence, available = await _channelguard_profile_links(bot, 101)

        self.assertTrue(available)
        self.assertEqual([item[0] for item in evidence], ["bio"])

    async def test_connected_channel_description_link_is_detected(self):
        bot = FakeBot({
            102: SimpleNamespace(
                bio="",
                personal_chat=SimpleNamespace(id=-102, username="updates_channel"),
            ),
            -102: SimpleNamespace(description="Visit https://example.com for details."),
        })

        evidence, available = await _channelguard_profile_links(bot, 102)

        self.assertTrue(available)
        self.assertEqual([item[0] for item in evidence], ["connected_channel_description"])


if __name__ == "__main__":
    unittest.main()
