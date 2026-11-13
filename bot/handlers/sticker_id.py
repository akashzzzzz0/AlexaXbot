from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import Message

from bot.config import settings

router = Router(name="owner_sticker_id")


def _custom_emoji_text(text: str, offset: int, length: int) -> str:
    """Extract an entity substring using Telegram's UTF-16 offset units."""
    encoded = text.encode("utf-16-le")
    start = offset * 2
    end = (offset + length) * 2
    try:
        return encoded[start:end].decode("utf-16-le")
    except UnicodeDecodeError:
        return ""


def _extract_custom_emoji_ids(text: str, entities) -> list[tuple[str, str]]:
    """Return preview text and custom emoji IDs from Telegram message entities."""
    matches = []
    for entity in entities or []:
        entity_type = getattr(entity.type, "value", entity.type)
        emoji_id = getattr(entity, "custom_emoji_id", None)
        if entity_type != "custom_emoji" or not emoji_id:
            continue
        preview = _custom_emoji_text(text, entity.offset, entity.length)
        matches.append((preview or "✨", str(emoji_id)))
    return matches


@router.message(Command("stickerid"))
async def get_sticker_file_id(message: Message) -> None:
    """Return a sticker file_id only to the configured owner in private chat."""
    owner_id = settings.effective_owner_id
    if not message.from_user or not owner_id or message.from_user.id != owner_id:
        return

    if message.chat.type != ChatType.PRIVATE:
        await message.answer("Privacy ke liye Alexa ko DM mein sticker bhejo, us sticker par reply karke /stickerid likho.")
        return

    target = message.reply_to_message
    sticker = target.sticker if target else None
    if not sticker:
        await message.answer(
            "Pehle Alexa ko sticker bhejo, phir us sticker par reply karke /stickerid likho."
        )
        return

    await message.answer(
        "Sticker file_id (copy this):\n"
        f"{sticker.file_id}\n\n"
        "Sticker unique ID (usually configuration mein use nahi hoti):\n"
        f"{sticker.file_unique_id}"
    )


@router.message(Command("emojiid"))
async def get_custom_emoji_ids(message: Message) -> None:
    """Extract Telegram premium/custom emoji IDs from a replied-to message for the owner."""
    owner_id = settings.effective_owner_id
    if not message.from_user or not owner_id or message.from_user.id != owner_id:
        return

    if message.chat.type != ChatType.PRIVATE:
        await message.answer(
            "Privacy ke liye Alexa ko DM mein emoji wala message bhejo, "
            "us par reply karke /emojiid likho."
        )
        return

    target = message.reply_to_message
    if target is None:
        await message.answer(
            "Premium emoji wale message par reply karke /emojiid bhejo."
        )
        return

    source_text = target.text or target.caption or ""
    entities = target.entities or target.caption_entities or []
    matches = _extract_custom_emoji_ids(source_text, entities)

    if not matches:
        await message.answer(
            "Is message mein Telegram Custom Emoji entity nahi mili.\n"
            "Premium emoji ko text message mein send karo, phir us message par "
            "reply karke /emojiid bhejo. Sticker ki ID ke liye /stickerid use karo."
        )
        return

    # Keep each response comfortably below Telegram's message length limit.
    for start in range(0, len(matches), 20):
        chunk = matches[start:start + 20]
        lines = [
            f"{index}. {preview}\ncustom_emoji_id: {emoji_id}"
            for index, (preview, emoji_id) in enumerate(chunk, start=start + 1)
        ]
        suffix = (
            f"\n\nTotal custom emojis found: {len(matches)}"
            if start == 0 else ""
        )
        await message.answer(
            "✨ <b>Custom Emoji IDs</b>\n\n"
            + "\n\n".join(lines)
            + suffix,
            parse_mode="HTML",
        )
