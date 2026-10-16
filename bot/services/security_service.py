import re
import random
import string
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
from aiogram import Bot
from aiogram.types import Message, ChatPermissions
from bot.database.models import SecuritySettings, CaptchaChallenge, GroupMember
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
LINK_PATTERN = re.compile(r"https?://[^\s]+|www\.[^\s]+|(?<![\w])@[A-Za-z][A-Za-z0-9_]{4,31}\b", re.IGNORECASE)
PHONE_PATTERN = re.compile(r"(?:\+?\d[\d\s\-]{7,}\d)")
NSFW_WORDS = {"nsfw", "porn", "xxx", "sex", "nude", "naked"}
AD_WORDS = {"subscribe", "follow my channel", "join my channel", "promo", "promotion", "advertisement", "buy now", "limited offer", "dm me", "earn money fast"}
CRYPTO_WORDS = {"crypto", "bitcoin", "ethereum", "usdt", "airdrop", "wallet connect", "seed phrase", "private key"}
_recent_texts = defaultdict(deque)
_message_windows = defaultdict(deque)
_join_windows = defaultdict(deque)
_leave_windows = defaultdict(deque)


def contains_invite_link(text: str) -> bool:
    return bool(INVITE_PATTERN.search(text))


def contains_link(text: str) -> bool:
    return bool(LINK_PATTERN.search(text))


def contains_phone_number(text: str) -> bool:
    return bool(PHONE_PATTERN.search(text))


def contains_scam(text: str) -> bool:
    return any(re.search(p, text) for p in SCAM_PATTERNS)


def contains_nsfw(text: str) -> bool:
    lower = text.lower()
    return any(word in lower for word in NSFW_WORDS)


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
    if security.anti_invite and contains_invite_link(text):
        violations.append("invite_link")
    if security.anti_link and contains_link(text):
        violations.append("link")
    if security.anti_phone and contains_phone_number(text):
        violations.append("phone_number")
    if security.anti_scam and contains_scam(text):
        violations.append("scam")
    if security.anti_crypto_scam and any(word in text.lower() for word in CRYPTO_WORDS) and contains_scam(text):
        violations.append("crypto_scam")
    if security.anti_advertisement and any(word in text.lower() for word in AD_WORDS):
        violations.append("advertisement")
    if security.anti_channel_promo and (contains_invite_link(text) or ("@" in text and any(word in text.lower() for word in ("subscribe", "join channel", "follow channel")))):
        violations.append("channel_promotion")
    if security.anti_forward_spam and getattr(message, "forward_origin", None):
        violations.append("forward_spam")
    if security.anti_username_spam and message.from_user.username and re.search(r"(.)\1{5,}", message.from_user.username.lower()):
        violations.append("suspicious_username")
    if security.anti_fake and not message.from_user.username and re.search(r"https?://|t\.me/|\+\d{7,}", text.lower()):
        violations.append("suspicious_account")
    if security.anti_nsfw and contains_nsfw(text):
        violations.append("nsfw")
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
    if security.lock_links and contains_link(text):
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
) -> CaptchaChallenge:
    answer = None
    if captcha_type == "math":
        _, answer = generate_math_question()

    expires_at = datetime.now(timezone.utc) + timedelta(seconds=timeout)
    challenge = CaptchaChallenge(
        group_id=group_id,
        user_id=user_id,
        challenge_type=captcha_type,
        answer=answer,
        expires_at=expires_at,
    )
    session.add(challenge)
    await session.flush()
    return challenge


async def verify_captcha(
    session: AsyncSession,
    group_id: int,
    user_id: int,
    answer: Optional[str] = None,
) -> bool:
    challenge = await session.scalar(
        select(CaptchaChallenge).where(
            and_(
                CaptchaChallenge.group_id == group_id,
                CaptchaChallenge.user_id == user_id,
                CaptchaChallenge.is_solved == False,
            )
        ).order_by(CaptchaChallenge.created_at.desc())
    )
    if not challenge:
        return False
    if datetime.now(timezone.utc) > challenge.expires_at.replace(tzinfo=timezone.utc):
        return False
    if challenge.challenge_type == "button":
        challenge.is_solved = True
        return True
    if answer and challenge.answer:
        if answer.strip() == challenge.answer.strip():
            challenge.is_solved = True
            return True
    return False


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
