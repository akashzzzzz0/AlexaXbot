"""Regression tests for content-lock enforcement. Run after installing requirements."""
import unittest
from types import SimpleNamespace

from bot.database.models import SecuritySettings
from bot.services.security_service import check_message_violations


def fake_message(text="hello", *, is_bot=False, markup=None):
    return SimpleNamespace(
        text=text,
        caption=None,
        from_user=SimpleNamespace(id=123, is_bot=is_bot, username=None),
        chat=SimpleNamespace(id=-100123),
        forward_origin=None,
        reply_markup=markup,
        photo=None,
        video=None,
        video_note=None,
        animation=None,
        dice=None,
        game=None,
        voice=None,
        audio=None,
        document=None,
        sticker=None,
        poll=None,
        location=None,
        contact=None,
    )


class ContentLockTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.security = SecuritySettings(
            group_id=-100123,
            anti_spam=False, anti_flood=False, anti_invite=False, anti_link=False,
            anti_phone=False, anti_scam=False, anti_crypto_scam=False,
            anti_advertisement=False, anti_channel_promo=False,
            anti_forward_spam=False, anti_username_spam=False, anti_fake=False,
            anti_nsfw=False, anti_emoji_spam=False, anti_mention_spam=False,
            anti_hashtag_spam=False, anti_raid=False, anti_bot=False,
            anti_mass_join=False, anti_mass_leave=False,
            lock_text=False, lock_links=False, lock_bots=True, lock_inline=True,
            lock_mentions=True, lock_hashtags=True, lock_photos=False,
            lock_videos=False, lock_gifs=False, lock_games=False, lock_voice=False,
            lock_audio=False, lock_documents=False, lock_stickers=False,
            lock_polls=False, lock_forwards=False, lock_locations=False,
            lock_contacts=False,
        )

    async def test_mentions_are_locked(self):
        self.assertIn("locked_mention", await check_message_violations(fake_message("hello @someone"), self.security))

    async def test_hashtags_are_locked(self):
        self.assertIn("locked_hashtag", await check_message_violations(fake_message("hello #topic"), self.security))

    async def test_inline_keyboard_is_locked(self):
        markup = SimpleNamespace(inline_keyboard=[[object()]])
        self.assertIn("locked_inline", await check_message_violations(fake_message(markup=markup), self.security))

    async def test_bot_messages_are_locked(self):
        self.assertIn("locked_bot", await check_message_violations(fake_message(is_bot=True), self.security))


if __name__ == "__main__":
    unittest.main()
