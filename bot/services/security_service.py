import re
import random
import string
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, update
from sqlalchemy.exc import IntegrityError
from aiogram import Bot
from aiogram.types import Message, ChatPermissions
from bot.database.models import SecuritySettings, CaptchaChallenge, GroupMember, Group
import structlog

logger = structlog.get_logger()

SCAM_PATTERNS = [
    r"(?i)(free\s+crypto|claim\s+your\s+reward|airdrop|giveaway\s+crypto)",
    r"(?i)(investment\s+opportunity|guaranteed\s+profit|passive\s+income)",
    r"(?i)(click\s+here\s+to\s+win|you\s+have\s+been\s+selected)",
    r"(?i)(send\s+\d+\s+(?:btc|eth|usdt|sol))",
    r"(?i)(double\s+your\s+(?:bitcoin|crypto|money))",
]

INVITE_PATTERN = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me)/(?:joinchat/|\+)[a-zA-Z0-9_-]+")
LINK_PATTERN = re.compile(
    r"(?:https?://|www\.)[^\s]+|(?<![\w])(?:t\.me|telegram\.me)/[^\s]+|(?<![\w])@[A-Za-z0-9_]{1,32}\b|(?<![\w])[A-Za-z0-9_-]{5,}\.t\.me\b|(?<![@\w.-])(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}(?::\d+)?(?:/[^\s]*)?",
    re.IGNORECASE,
)
PHONE_PATTERN = re.compile(r"(?:\+?\d[\d\s\-]{7,}\d)")
# Text/caption keyword screening. Keep word boundaries so ordinary words such as
# "classic" do not trigger on fragments like "ass".
NSFW_WORDS = {
    "nsfw", "porn", "porno", "xxx", "sex", "nude", "nudes", "naked",
    "hentai", "erotic", "onlyfans", "sexual content",
}
# Common English, Hindi, and Roman Urdu abuse terms. This is a practical
# moderation list, not an exhaustive dictionary; group admins can still use
# Telegram permissions and custom blocklists for local slang.
ABUSE_WORDS = {
    "fuck", "fucking", "fucked", "motherfucker", "mf", "shit", "bullshit",
    "bitch", "bastard", "asshole", " asshole", "dickhead", "dick", "piss off",
    "son of a bitch", "cunt", "slut", "whore", "idiot", "moron",
    "madarchod", "mc", "behenchod", "bc", "bhenchod", "bhosdike", "bhosdi",
    "chutiya", "chutiye", " chut", "gaand", "gandu", "lodu", "lund",
    "randi", "harami", "kamina", "kaminey", " kutte", "saala", "saali",
    "bsdk", "bkl", "mkc", "lawde", "lavde", "jhatu", "bakchod",
    "gadha", "ullu", "kam**na",
    "حرامی", "بکواس", "کمینہ", "کمینی", "بےوقوف", "چوتیا",
    "मादरचोद", "बहनचोद", "भोसड़ी", "चूतिया", "गांडू", "हरामी",
}
def _moderation_text(text: str) -> str:
    return re.sub(r"[^\w]+", " ", (text or "").casefold(), flags=re.UNICODE).strip()

def _contains_word_set(text: str, words: set[str]) -> bool:
    normalized = _moderation_text(text)
    if not normalized:
        return False
    padded = f" {normalized} "
    for word in words:
        candidate = _moderation_text(word)
        if candidate and f" {candidate} " in padded:
            return True
    return False
AD_WORDS = {"subscribe", "follow my channel", "join my channel", "promo", "promotion", "advertisement", "buy now", "limited offer", "dm me", "earn money fast"}
CRYPTO_WORDS = {"crypto", "bitcoin", "ethereum", "usdt", "airdrop", "wallet connect", "seed phrase", "private key"}
_recent_texts = defaultdict(deque)
_message_windows = defaultdict(deque)
_join_windows = defaultdict(deque)
_leave_windows = defaultdict(deque)


# Cache completed sticker decisions so the same sticker is not sent to the
# moderation API repeatedly. The cache is process-local and bounded.
_sticker_moderation_cache: dict[str, bool] = {}


async def is_nsfw_sticker(bot: Bot, sticker) -> bool:
    """Use OpenAI image moderation to identify sexual content in Telegram stickers."""
    import base64
    import io
    import os

    from PIL import Image
    import httpx
    from bot.config import settings

    sticker_key = getattr(sticker, "file_unique_id", None)
    if sticker_key and sticker_key in _sticker_moderation_cache:
        return _sticker_moderation_cache[sticker_key]

    api_key = (settings.OPENAI_API_KEY or os.getenv("OPENAI_API_KEY", "")).strip()
    if not api_key:
        logger.warning("Automatic sticker moderation skipped: OPENAI_API_KEY is not configured")
        return False

    # Telegram animated/video stickers expose a static thumbnail. Static
    # stickers are downloaded directly. If no preview exists, fail open.
    source_file = sticker
    if getattr(sticker, "is_animated", False) or getattr(sticker, "is_video", False):
        source_file = getattr(sticker, "thumbnail", None) or getattr(sticker, "thumb", None)
        if source_file is None:
            logger.info("Sticker moderation skipped: animated sticker has no thumbnail")
            return False

    if (getattr(source_file, "file_size", None) or 0) > 5 * 1024 * 1024:
        logger.info("Sticker moderation skipped: image exceeds 5 MB")
        return False

    try:
        telegram_file = await bot.get_file(source_file.file_id)
        raw = io.BytesIO()
        await bot.download_file(telegram_file.file_path, destination=raw)
        raw.seek(0)

        # Normalize Telegram WEBP/thumbnail data into a compact JPEG data URL.
        with Image.open(raw) as opened:
            image = opened.convert("RGB")
            image.thumbnail((768, 768))
            encoded = io.BytesIO()
            image.save(encoded, format="JPEG", quality=85, optimize=True)
        image_data = base64.b64encode(encoded.getvalue()).decode("ascii")

        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
            response = await client.post(
                "https://api.openai.com/v1/moderations",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": "omni-moderation-latest",
                    "input": [{
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{image_data}"},
                    }],
                },
            )
            response.raise_for_status()
            result = response.json()["results"][0]
            categories = result.get("categories", {})
            detected = bool(
                categories.get("sexual", False)
                or categories.get("sexual/minors", False)
            )

        if sticker_key:
            if len(_sticker_moderation_cache) >= 2000:
                _sticker_moderation_cache.pop(next(iter(_sticker_moderation_cache)))
            _sticker_moderation_cache[sticker_key] = detected
        return detected
    except Exception as exc:
        # Do not delete a sticker merely because the API/network failed.
        logger.warning(
            "Automatic sticker moderation failed; sticker left untouched",
            sticker_id=sticker_key,
            error=str(exc),
        )
        return False


def contains_invite_link(text: str) -> bool:
    return bool(INVITE_PATTERN.search(text))


def contains_link(text: str) -> bool:
    return bool(LINK_PATTERN.search(text))


def contains_non_alexa_link(text: str) -> bool:
    """Bio-link protection ignores ALEXA's official bot link, but still catches any other link."""
    # Remove the official bot username/link, including referral query parameters,
    # before checking the remaining bio content for links.
    alexa_link_pattern = re.compile(
        r"(?i)(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/?AlexaSuper_Bot(?:\?[^\s]*)?|@AlexaSuper_Bot\b"
    )
    remaining = alexa_link_pattern.sub("", text)
    return contains_link(remaining)


def contains_non_invite_link(text: str) -> bool:
    """Detect ordinary links without making the Anti-Link switch duplicate Anti-Invite."""
    without_invites = INVITE_PATTERN.sub("", text)
    return contains_link(without_invites)


def contains_phone_number(text: str) -> bool:
    return bool(PHONE_PATTERN.search(text))


def contains_scam(text: str) -> bool:
    return any(re.search(p, text) for p in SCAM_PATTERNS)


def contains_nsfw(text: str) -> bool:
    return _contains_word_set(text, NSFW_WORDS)


def contains_abuse(text: str) -> bool:
    return _contains_word_set(text, ABUSE_WORDS)


def count_emojis(text: str) -> int:
    import unicodedata
    return sum(1 for c in text if unicodedata.category(c) in ("So", "Sm") or ord(c) > 0x1F300)


def count_mentions(text: str) -> int:
    return text.count("@")


def count_hashtags(text: str) -> int:
    return len(re.findall(r"#\w+", text))


async def check_message_violations(
    message: Message,
    security: SecuritySettings,
    approved_sticker_user: bool = False,
) -> list[str]:
    violations = []
    text = message.text or message.caption or ""

    # Telegram can hide a destination behind clickable text (text_link).
    # Include hidden destinations and URL entities in link detection.
    entities = list(getattr(message, "entities", None) or []) + list(getattr(message, "caption_entities", None) or [])
    entity_links = []
    for entity in entities:
        raw_type = getattr(entity, "type", None)
        entity_type = getattr(raw_type, "value", raw_type)
        if entity_type == "url":
            entity_links.append("url_entity")
        elif entity_type == "text_link" and getattr(entity, "url", None):
            entity_links.append(entity.url)
    link_scan_text = " ".join([text, *entity_links])

    # ALEXA must not moderate its own outbound messages (for example, its
    # official referral/promotion links). Other bots remain subject to the
    # configured group bot/content locks.
    sender = getattr(message, "from_user", None)
    bot = getattr(message, "bot", None)
    if sender is not None and bot is not None and sender.id == getattr(bot, "id", None):
        return []

    now = time.monotonic()
    key = (message.chat.id, message.from_user.id)
    if security.anti_spam and text.strip():
        q = _recent_texts[key]
        normalized = " ".join(text.lower().split())
        while q and now - q[0][0] > 30:
            q.popleft()
        if (len(normalized) >= 8 or contains_link(normalized)) and any(old_text == normalized for _, old_text in q):
            violations.append("duplicate_spam")
        q.append((now, normalized))
    if security.anti_flood:
        q = _message_windows[key]
        window = max(1, security.flood_window or 10)
        while q and now - q[0] > window:
            q.popleft()
        q.append(now)
        if len(q) > max(1, security.flood_limit or 5):
            violations.append("flood")
    # Keep these controls independent: Anti-Invite handles Telegram group invite
    # links, while Anti-Link handles other URLs/user links. A message containing
    # both kinds is still caught by both relevant controls.
    if security.anti_invite and contains_invite_link(text):
        violations.append("invite_link")
    if security.anti_link and (contains_link(link_scan_text) or "url_entity" in entity_links):
        violations.append("link")
    if security.anti_phone and contains_phone_number(text):
        violations.append("phone_number")
    if security.anti_scam and contains_scam(text):
        violations.append("scam")
    if security.anti_crypto_scam and any(word in text.lower() for word in CRYPTO_WORDS) and contains_scam(text):
        violations.append("crypto_scam")
    if security.anti_advertisement and any(word in text.lower() for word in AD_WORDS):
        violations.append("advertisement")
    # Invite links belong to Anti-Invite; this switch targets promotional
    # channel mentions/text and must not duplicate Anti-Invite's responsibility.
    if security.anti_channel_promo and ("@" in text and any(word in text.lower() for word in ("subscribe", "join channel", "follow channel"))):
        violations.append("channel_promotion")
    if security.anti_forward_spam and getattr(message, "forward_origin", None):
        violations.append("forward_spam")
    if security.anti_username_spam and message.from_user.username and re.search(r"(.)\1{5,}", message.from_user.username.lower()):
        violations.append("suspicious_username")
    if security.anti_fake and not message.from_user.username and re.search(r"https?://|t\.me/|\+\d{7,}", text.lower()):
        violations.append("suspicious_account")
    if security.anti_nsfw:
        if contains_nsfw(text):
            violations.append("nsfw")
        elif contains_abuse(text):
            violations.append("abuse")
    if security.anti_emoji_spam and count_emojis(text) > (security.anti_emoji_limit or 10):
        violations.append("emoji_spam")
    if security.anti_mention_spam and count_mentions(text) > (security.anti_mention_limit or 5):
        violations.append("mention_spam")
    if security.anti_hashtag_spam and count_hashtags(text) > 5:
        violations.append("hashtag_spam")
    # Content locks are independent from spam thresholds: a locked type must be
    # enforced even when the message is otherwise harmless.
    if security.lock_text and message.text and not message.caption:
        violations.append("locked_text")
    if security.lock_links and (contains_link(link_scan_text) or "url_entity" in entity_links):
        violations.append("locked_link")
    if security.lock_bots and message.from_user and message.from_user.is_bot:
        violations.append("locked_bot")
    if security.lock_inline and getattr(message, "reply_markup", None) and getattr(message.reply_markup, "inline_keyboard", None):
        violations.append("locked_inline")
    if security.lock_mentions and count_mentions(text) > 0:
        violations.append("locked_mention")
    if security.lock_hashtags and count_hashtags(text) > 0:
        violations.append("locked_hashtag")
    if security.lock_photos and message.photo:
        violations.append("locked_photo")
    if security.lock_videos and (message.video or getattr(message, "video_note", None)):
        violations.append("locked_video")
    if security.lock_gifs and getattr(message, "animation", None):
        violations.append("locked_gif")
    if security.lock_games and (getattr(message, "dice", None) or getattr(message, "game", None)):
        violations.append("locked_game")
    if security.lock_voice and message.voice:
        violations.append("locked_voice")
    if security.lock_audio and message.audio:
        violations.append("locked_audio")
    if security.lock_documents and message.document:
        violations.append("locked_document")
    if security.lock_stickers and message.sticker and not approved_sticker_user:
        violations.append("locked_sticker")
    if security.lock_polls and message.poll:
        violations.append("locked_poll")
    if security.lock_forwards and message.forward_origin:
        violations.append("locked_forward")
    if security.lock_locations and message.location:
        violations.append("locked_location")
    if security.lock_contacts and message.contact:
        violations.append("locked_contact")
    if security.lock_mentions and count_mentions(text) > 0:
        violations.append("locked_mention")
    if security.lock_hashtags and count_hashtags(text) > 0:
        violations.append("locked_hashtag")
    if security.lock_inline and getattr(message, "via_bot", None):
        violations.append("locked_inline")
    if security.lock_bots and getattr(message, "via_bot", None):
        violations.append("locked_bot")

    return list(dict.fromkeys(violations))


def generate_math_question() -> Tuple[str, str]:
    op = random.choice(["+", "-", "*"])
    if op == "*":
        a = random.randint(1, 10)
        b = random.randint(1, 10)
    else:
        a = random.randint(1, 20)
        b = random.randint(1, 20)
    if op == "+":
        answer = a + b
    elif op == "-":
        answer = abs(a - b)
        if a < b:
            a, b = b, a
    else:
        answer = a * b
    return f"{a} {op} {b}", str(answer)


async def create_captcha_challenge(
    session: AsyncSession,
    group_id: int,
    user_id: int,
    captcha_type: str,
    timeout: int = 60,
) -> Tuple[CaptchaChallenge, bool]:
    # Serialize challenge creation within a group. The group row already exists
    # before welcome handlers run, and PostgreSQL honors this row lock.
    await session.scalar(
        select(Group.id).where(Group.id == group_id).with_for_update()
    )
    # Invalidate any previous challenge before creating a fresh one.
    await session.execute(
        update(CaptchaChallenge)
        .where(
            CaptchaChallenge.group_id == group_id,
            CaptchaChallenge.user_id == user_id,
            CaptchaChallenge.is_solved.is_(False),
        )
        .values(is_solved=True)
    )

    answer = None
    if captcha_type == "math":
        _, answer = generate_math_question()

    expires_at = datetime.now(timezone.utc) + timedelta(seconds=max(1, timeout))
    challenge = CaptchaChallenge(
        group_id=group_id,
        user_id=user_id,
        challenge_type=captcha_type,
        answer=answer,
        expires_at=expires_at,
    )
    try:
        async with session.begin_nested():
            session.add(challenge)
            await session.flush()
        return challenge, True
    except IntegrityError:
        # A concurrent join already created the one active challenge.
        existing = await session.scalar(
            select(CaptchaChallenge).where(
                CaptchaChallenge.group_id == group_id,
                CaptchaChallenge.user_id == user_id,
                CaptchaChallenge.is_solved.is_(False),
            ).order_by(CaptchaChallenge.id.desc()).limit(1)
        )
        if existing is None:
            raise
        return existing, False


async def verify_captcha(
    session: AsyncSession,
    group_id: int,
    user_id: int,
    challenge_id: int,
    answer: Optional[str] = None,
) -> Optional[bool]:
    challenge = await session.scalar(
        select(CaptchaChallenge).where(
            CaptchaChallenge.id == challenge_id,
            CaptchaChallenge.group_id == group_id,
            CaptchaChallenge.user_id == user_id,
            CaptchaChallenge.is_solved.is_(False),
        )
    )
    if challenge is None:
        return None

    now = datetime.now(timezone.utc)
    expires_at = challenge.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if now >= expires_at:
        # Expire it atomically; a stale/expired button must not count as a wrong answer.
        await session.execute(
            update(CaptchaChallenge)
            .where(
                CaptchaChallenge.id == challenge_id,
                CaptchaChallenge.is_solved.is_(False),
            )
            .values(is_solved=True)
        )
        return None

    if challenge.challenge_type == "button":
        correct = answer == "button"
    else:
        correct = bool(answer and challenge.answer and answer.strip() == challenge.answer.strip())

    if not correct:
        return False

    # Compare-and-set: only one concurrent callback can consume this challenge.
    claimed = await session.execute(
        update(CaptchaChallenge)
        .where(
            CaptchaChallenge.id == challenge_id,
            CaptchaChallenge.group_id == group_id,
            CaptchaChallenge.user_id == user_id,
            CaptchaChallenge.is_solved.is_(False),
            CaptchaChallenge.expires_at > now,
        )
        .values(is_solved=True)
        .execution_options(synchronize_session=False)
    )
    return True if claimed.rowcount == 1 else None


async def is_raid_detected(
    redis_client,
    group_id: int,
    threshold: int = 10,
    window: int = 60,
) -> bool:
    if redis_client is not None:
        try:
            key = f"raid:{group_id}"
            count = await redis_client.incr(key)
            if count == 1:
                await redis_client.expire(key, window)
            return count >= threshold
        except Exception as exc:
            logger.warning("Redis raid counter failed; using local counter", error=str(exc))
    now = time.monotonic()
    q = _join_windows[group_id]
    while q and now - q[0] > max(1, window):
        q.popleft()
    q.append(now)
    return len(q) >= max(1, threshold)


def record_member_leave(group_id: int, threshold: int = 10, window: int = 60) -> bool:
    now = time.monotonic()
    q = _leave_windows[group_id]
    while q and now - q[0] > max(1, window):
        q.popleft()
    q.append(now)
    return len(q) >= max(1, threshold)


def clear_flood_state(group_id: int) -> int:
    """Clear in-memory flood counters for a group; return number of counters cleared."""
    keys = [key for key in _message_windows if key[0] == group_id]
    for key in keys:
        _message_windows.pop(key, None)
    return len(keys)
