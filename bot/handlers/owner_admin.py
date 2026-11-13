"""Owner-ID-only group administrator promotion command.

The configured bot owner can reply to a member's message with /admin.
ALEXA must itself have Telegram's can_promote_members permission.
"""
from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
import structlog

from bot.config import settings

logger = structlog.get_logger()
router = Router()


def _is_owner(user_id: int) -> bool:
    return bool(settings.effective_owner_id) and user_id == settings.effective_owner_id


@router.message(Command("admin"))
async def owner_promote_member(message: Message):
    if not message.from_user or not _is_owner(message.from_user.id):
        return

    if message.chat.type not in {"group", "supergroup"}:
        await message.reply("❌ /admin can only be used in a group.")
        return

    target_message = message.reply_to_message
    target = target_message.from_user if target_message else None
    if not target:
        await message.reply("⚠️ Kisi member ke message par reply karke /admin bhejo.")
        return
    if target.is_bot:
        await message.reply("❌ Bot accounts ko is command se Admin nahi banaya ja sakta.")
        return
    if target.id == message.bot.id:
        await message.reply("❌ Alexa ko khud promote nahi kiya ja sakta.")
        return

    try:
        bot_member = await message.bot.get_chat_member(message.chat.id, message.bot.id)
        bot_status = str(getattr(bot_member.status, "value", bot_member.status))
        if bot_status != "creator" and not (
            bot_status == "administrator"
            and bool(getattr(bot_member, "can_promote_members", False))
        ):
            await message.reply(
                "❌ Alexa ke paas Promote Members permission nahi hai. "
                "Group settings mein Alexa ko ye permission do."
            )
            return

        member = await message.bot.get_chat_member(message.chat.id, target.id)
        member_status = str(getattr(member.status, "value", member.status))
        if member_status in {"creator", "administrator"}:
            await message.reply("ℹ️ Ye user pehle se Admin hai.")
            return

        # Grant useful moderation powers, but do not allow this new admin
        # to promote other admins or change group ownership.
        promoted = await message.bot.promote_chat_member(
            chat_id=message.chat.id,
            user_id=target.id,
            can_delete_messages=True,
            can_restrict_members=True,
            can_invite_users=True,
            can_pin_messages=True,
            can_manage_video_chats=True,
            can_manage_topics=True,
            can_change_info=True,
            can_promote_members=False,
        )
        if promoted:
            name = target.full_name.replace("<", "&lt;").replace(">", "&gt;")
            await message.reply(
                f"✅ <b>{name}</b> ko Admin promote kar diya gaya.\n"
                "🔒 Promote Members permission enabled nahi ki gayi.",
                parse_mode="HTML",
            )
        else:
            await message.reply("❌ Telegram ne promotion complete nahi ki. Alexa ki permissions check karo.")
    except TelegramForbiddenError:
        await message.reply("❌ Telegram ne action reject kiya. Alexa ki Admin/Promote Members permission check karo.")
    except TelegramBadRequest as exc:
        logger.warning(
            "Owner admin promotion rejected by Telegram",
            chat_id=message.chat.id,
            target_user_id=target.id,
            error=str(exc),
        )
        await message.reply(f"❌ Telegram promotion reject kar raha hai: {str(exc)[:250]}")
    except Exception as exc:
        logger.exception(
            "Owner admin promotion failed",
            chat_id=message.chat.id,
            target_user_id=target.id,
            error=str(exc),
        )
        await message.reply("❌ Admin promotion fail ho gayi. Logs aur Alexa ki permissions check karo.")
