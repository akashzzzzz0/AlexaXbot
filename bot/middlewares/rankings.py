"""Best-effort message capture for the isolated group rankings database."""
from aiogram import BaseMiddleware
import structlog

logger = structlog.get_logger()


class RankingsTrackingMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        try:
            from bot.services.rankings_store import record_group_message
            await record_group_message(event)
        except Exception as exc:
            # Analytics must never interrupt moderation or normal group handling.
            logger.warning("Group rankings tracking failed", error=str(exc))
        return await handler(event, data)
