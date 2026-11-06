"""Private acknowledgements for Telegram group join requests.

This handler never posts to the group and never approves/rejects a request.
The existing welcome handler remains responsible for greeting a member only
after Telegram reports the actual JOIN_TRANSITION.
"""
from html import escape

import structlog
from aiogram import Router
from aiogram.types import ChatJoinRequest

logger = structlog.get_logger()
router = Router()


@router.chat_join_request()
async def acknowledge_join_request(request: ChatJoinRequest, **kwargs):
    """DM the requester using Telegram's temporary user_chat_id permission."""
    group_name = escape(request.chat.title or "the group")
    first_name = escape(request.from_user.first_name or "there")
    try:
        await request.bot.send_message(
            chat_id=request.user_chat_id,
            text=(
                f"👋 Hello {first_name}!\n\n"
                f"Thanks for requesting to join <b>{group_name}</b>. "
                "Your request has been received and is waiting for an admin's review.\n\n"
                "You do not need to send anything else. If an admin approves your request, "
                "you will be added to the group.\n\n"
                "✨ Want free group promotion? Add ALEXA to your own group, "
                "then send your group link here in this DM."
            ),
            parse_mode="HTML",
        )
        logger.info("Join-request acknowledgement sent privately", chat_id=request.chat.id, user_id=request.from_user.id)
    except Exception as exc:
        # A failed DM must not alter the pending request or produce a group message.
        logger.info("Could not DM join requester; request remains pending", chat_id=request.chat.id, user_id=request.from_user.id, user_chat_id=request.user_chat_id, error=str(exc))
