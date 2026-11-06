"""Private acknowledgements and activation for Telegram group join requests.

This handler never posts to the group and never approves/rejects a request.
The existing welcome handler remains responsible for greeting a member only
after Telegram reports the actual JOIN_TRANSITION.
"""
from html import escape

import structlog
from aiogram import Router, F
from aiogram.types import ChatJoinRequest, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

logger = structlog.get_logger()
router = Router()


@router.chat_join_request()
async def acknowledge_join_request(request: ChatJoinRequest, **kwargs):
    """DM the requester using Telegram's temporary user_chat_id permission."""
    group_name = escape(request.chat.title or "the group")
    first_name = escape(request.from_user.first_name or "there")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="✅ Verify & Start Alexa",
            callback_data="joinreq:verify",
        )
    ]])
    try:
        await request.bot.send_message(
            chat_id=request.user_chat_id,
            text=(
                f"👋 Hello {first_name}!\n\n"
                f"Thanks for requesting to join <b>{group_name}</b>. "
                "Your request has been received and is waiting for an admin's review.\n\n"
                "Tap the button below to verify and activate your ALEXA private chat. "
                "Alexa will continue right here in this DM.\n\n"
                "✨ Want free group promotion? Add ALEXA to your own group, "
                "then send your group link here in this DM."
            ),
            parse_mode="HTML",
            reply_markup=keyboard,
        )
        logger.info("Join-request acknowledgement sent privately", chat_id=request.chat.id, user_id=request.from_user.id)
    except Exception as exc:
        # A failed DM must not alter the pending request or produce a group message.
        logger.info("Could not DM join requester; request remains pending", chat_id=request.chat.id, user_id=request.from_user.id, user_chat_id=request.user_chat_id, error=str(exc))


@router.callback_query(F.data == "joinreq:verify")
async def activate_join_request_dm(
    callback: CallbackQuery,
    db_session=None,
    db_user=None,
    **kwargs,
):
    """Treat the explicit button tap as the user's first ALEXA DM activation."""
    if not callback.message or callback.message.chat.type != "private":
        await callback.answer("Please use this button in your private chat with ALEXA.", show_alert=True)
        return
    if callback.from_user.id != callback.message.chat.id:
        await callback.answer("This verification button belongs to the requester.", show_alert=True)
        return

    if db_user is not None:
        db_user.dm_started = True
        if db_session is not None:
            await db_session.flush()

    await callback.answer("✅ Verified! ALEXA is active in this DM.")
    try:
        await callback.message.delete()
    except Exception as exc:
        logger.info("Could not remove join-request verification message", user_id=callback.from_user.id, error=str(exc))

    try:
        # Reuse the existing /start welcome experience and its existing buttons.
        # Pass callback.from_user because callback.message.from_user is the bot.
        from bot.handlers.start import _send_add_group_prompt
        await _send_add_group_prompt(callback.message, db_user, user=callback.from_user)
        logger.info("Join-request DM activated", user_id=callback.from_user.id)
    except Exception as exc:
        logger.warning("Could not show ALEXA welcome after join-request verification", user_id=callback.from_user.id, error=str(exc))
        await callback.bot.send_message(
            callback.message.chat.id,
            "✅ Verification complete! ALEXA is active in this private chat. Send your message here anytime.",
        )
