"""Regression tests for ChannelGuard's exposed profile-link detection."""
import unittest
from types import SimpleNamespace

from bot.handlers.welcome import _channelguard_profile_links


class FakeBot:
    def __init__(self, chats):
        self.chats = chats
        self.calls = 0

    async def get_chat(self, chat_id):
        self.calls += 1
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


    async def test_successful_profile_reads_are_cached_for_repeated_messages(self):
        bot = FakeBot({
            103: SimpleNamespace(bio="https://example.com", personal_chat=None),
        })

        first = await _channelguard_profile_links(bot, 103)
        second = await _channelguard_profile_links(bot, 103)

        self.assertEqual(first, second)
        self.assertEqual(bot.calls, 1)

    async def test_force_refresh_bypasses_profile_cache_after_bio_cleanup(self):
        profile = SimpleNamespace(bio="https://example.com", personal_chat=None)
        bot = FakeBot({104: profile})
        first, available = await _channelguard_profile_links(bot, 104)
        self.assertTrue(available)
        self.assertTrue(first)

        profile.bio = ""
        refreshed, available = await _channelguard_profile_links(bot, 104, force_refresh=True)

        self.assertTrue(available)
        self.assertEqual(refreshed, [])
        self.assertEqual(bot.calls, 2)


if __name__ == "__main__":
    unittest.main()
