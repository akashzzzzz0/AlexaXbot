from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import Message

from bot.config import settings

router = Router(name="owner_sticker_id")


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
