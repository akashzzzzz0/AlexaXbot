"""Regression tests for content-lock enforcement. Run after installing requirements."""
import unittest
from types import SimpleNamespace

from bot.database.models import SecuritySettings
from bot.services.security_service import check_message_violations


def fake_message(text="hello", *, is_bot=False, markup=None, sender_id=123, bot_id=None):
    message = SimpleNamespace(
        text=text,
        caption=None,
        entities=None,
        caption_entities=None,
        from_user=SimpleNamespace(id=sender_id, is_bot=is_bot, username=None),
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
    if bot_id is not None:
        message.bot = SimpleNamespace(id=bot_id)
    return message


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

    async def test_alexas_own_link_is_not_moderated(self):
        self.security.anti_link = True
        own_message = fake_message("https://t.me/alexa_bot_updats", is_bot=True, sender_id=999, bot_id=999)
        self.assertEqual(await check_message_violations(own_message, self.security), [])

    async def test_other_bots_still_follow_group_locks(self):
        self.security.anti_link = True
        other_bot_message = fake_message("https://t.me/example", is_bot=True, sender_id=456, bot_id=999)
        violations = await check_message_violations(other_bot_message, self.security)
        self.assertIn("link", violations)
        self.assertIn("locked_bot", violations)

    async def test_approved_member_can_send_sticker_even_when_all_locks_are_enabled(self):
        for field in (
            "lock_text", "lock_links", "lock_photos", "lock_videos", "lock_gifs",
            "lock_audio", "lock_voice", "lock_documents", "lock_polls", "lock_games",
            "lock_bots", "lock_forwards", "lock_contacts", "lock_locations",
            "lock_stickers", "lock_inline", "lock_mentions", "lock_hashtags",
        ):
            setattr(self.security, field, True)
        message = fake_message()
        message.text = None
        message.sticker = object()
        violations = await check_message_violations(message, self.security, approved_sticker_user=True)
        self.assertNotIn("locked_sticker", violations)

    async def test_non_approved_member_still_cannot_send_sticker_when_locked(self):
        self.security.lock_stickers = True
        message = fake_message()
        message.text = None
        message.sticker = object()
        violations = await check_message_violations(message, self.security, approved_sticker_user=False)
        self.assertIn("locked_sticker", violations)

    async def test_anti_link_toggle_detects_regular_urls_only_when_enabled(self):
        message = fake_message("https://example.com")
        self.security.anti_link = False
        self.assertNotIn("link", await check_message_violations(message, self.security))
        self.security.anti_link = True
        self.assertIn("link", await check_message_violations(message, self.security))

    async def test_anti_link_detects_bare_domains(self):
        self.security.anti_link = True
        for value in ("example.com", "sub.example.co.uk/path", "example.com:8080"):
            with self.subTest(value=value):
                self.assertIn("link", await check_message_violations(fake_message(value), self.security))

    async def test_anti_link_detects_hidden_text_links(self):
        self.security.anti_link = True
        entity = SimpleNamespace(type="text_link", url="https://hidden.example/path")
        message = fake_message("click here")
        message.entities = [entity]
        self.assertIn("link", await check_message_violations(message, self.security))

    async def test_anti_link_detects_url_entities(self):
        self.security.anti_link = True
        entity = SimpleNamespace(type="url", url=None)
        message = fake_message("example.com")
        message.entities = [entity]
        self.assertIn("link", await check_message_violations(message, self.security))

    async def test_anti_invite_toggle_detects_telegram_invites_only_when_enabled(self):
        message = fake_message("Join https://t.me/+AbCdEf123")
        self.security.anti_invite = False
        self.assertNotIn("invite_link", await check_message_violations(message, self.security))
        self.security.anti_invite = True
        self.assertIn("invite_link", await check_message_violations(message, self.security))

    async def test_anti_scam_toggle(self):
        message = fake_message("Claim your reward now")
        self.security.anti_scam = False
        self.assertNotIn("scam", await check_message_violations(message, self.security))
        self.security.anti_scam = True
        self.assertIn("scam", await check_message_violations(message, self.security))

    async def test_anti_advertisement_toggle(self):
        message = fake_message("Subscribe to my channel")
        self.security.anti_advertisement = False
        self.assertNotIn("advertisement", await check_message_violations(message, self.security))
        self.security.anti_advertisement = True
        self.assertIn("advertisement", await check_message_violations(message, self.security))

    async def test_anti_spam_toggle(self):
        message1 = fake_message("Repeated message text")
        message2 = fake_message("Repeated message text")
        self.security.anti_spam = True
        await check_message_violations(message1, self.security)
        self.assertIn("duplicate_spam", await check_message_violations(message2, self.security))
        self.security.anti_spam = False
        message3 = fake_message("Repeated message text")
        self.assertNotIn("duplicate_spam", await check_message_violations(message3, self.security))

    async def test_anti_flood_toggle(self):
        self.security.anti_flood = True
        self.security.flood_limit = 1
        self.security.flood_window = 10
        first = fake_message("one", sender_id=321)
        second = fake_message("two", sender_id=321)
        self.assertNotIn("flood", await check_message_violations(first, self.security))
        self.assertIn("flood", await check_message_violations(second, self.security))


if __name__ == "__main__":
    unittest.main()
