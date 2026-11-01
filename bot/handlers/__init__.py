from aiogram import Router
from bot.handlers import start, security, moderation, economy, games, duels, statistics, settings, help, affiliate, owner, owner_group_message, owner_group_activity, force_join, welcome, tournaments, alexa_games, group_tools, community_shield, scam_review, ultra_vip, ai, ai_chat, owner_ai_control, missing_games, group_promotion, sticker_id, world_events
from bot.handlers.auto_delete_middleware import StartMenuAutoDeleteMessageMiddleware, StartMenuAutoDeleteCallbackMiddleware

class DailyGroupMessageCounterMiddleware:
    """Count group messages and keep the owner-panel group registry reconciled."""
    def __init__(self):
        self._known_group_ids = set()

    async def __call__(self, handler, event, data):
        message = event
        chat_type = getattr(getattr(message, "chat", None), "type", None)
        has_content = any(getattr(message, field, None) for field in (
            "text", "caption", "photo", "document", "sticker", "video", "audio", "voice", "animation"
        ))
        # Telegram does not provide a Bot API method to enumerate every chat
        # the bot belongs to. Therefore, reconcile the registry whenever any
        # message arrives from a group; this also backfills groups whose
        # my_chat_member update was missed before this deployment.
        if (
            chat_type in {"group", "supergroup"}
            and has_content
            and message.chat.id not in self._known_group_ids
        ):
            try:
                from bot.database.connection import async_session_maker
                from bot.database.models import Group
                async with async_session_maker() as session:
                    group = await session.get(Group, message.chat.id)
                    changed = False
                    if group is None:
                        session.add(Group(
                            id=message.chat.id,
                            title=message.chat.title or "Unknown",
                            username=message.chat.username,
                            type=chat_type,
                            is_active=True,
                        ))
                        changed = True
                    else:
                        title = message.chat.title or group.title or "Unknown"
                        if group.title != title:
                            group.title = title
                            changed = True
                        if group.username != message.chat.username:
                            group.username = message.chat.username
                            changed = True
                        if group.type != chat_type:
                            group.type = chat_type
                            changed = True
                        if not group.is_active:
                            group.is_active = True
                            changed = True
                    if changed:
                        await session.commit()
                self._known_group_ids.add(message.chat.id)
            except Exception:
                # Registry reconciliation is best-effort and must never block
                # normal moderation or group message processing.
                pass

        if (
            chat_type in {"group", "supergroup"}
            and getattr(message, "from_user", None)
            and not message.from_user.is_bot
            and has_content
        ):
            from datetime import datetime
            from zoneinfo import ZoneInfo
            from sqlalchemy import select
            from sqlalchemy.exc import IntegrityError
            from bot.database.connection import async_session_maker
            from bot.database.models import DailyGroupMessageStat
            day_key = datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()
            try:
                async with async_session_maker() as session:
                    row = await session.scalar(select(DailyGroupMessageStat).where(
                        DailyGroupMessageStat.group_id == message.chat.id,
                        DailyGroupMessageStat.day_key == day_key,
                    ))
                    if row:
                        row.message_count = (row.message_count or 0) + 1
                    else:
                        session.add(DailyGroupMessageStat(
                            group_id=message.chat.id, day_key=day_key, message_count=1
                        ))
                    await session.commit()
            except IntegrityError:
                try:
                    async with async_session_maker() as session:
                        row = await session.scalar(select(DailyGroupMessageStat).where(
                            DailyGroupMessageStat.group_id == message.chat.id,
                            DailyGroupMessageStat.day_key == day_key,
                        ))
                        if row:
                            row.message_count = (row.message_count or 0) + 1
                            await session.commit()
                except Exception:
                    pass
            except Exception:
                # Never interrupt group handlers if optional stats tracking fails.
                pass
        return await handler(event, data)



def get_main_router() -> Router:
    router = Router()
    router.message.outer_middleware(DailyGroupMessageCounterMiddleware())
    router.message.outer_middleware(StartMenuAutoDeleteMessageMiddleware())
    router.callback_query.outer_middleware(StartMenuAutoDeleteCallbackMiddleware())
    # Register command and feature routers before welcome's broad group-message
    # fallback. The fallback belongs last so it cannot intercept group commands
    # or callbacks intended for the other feature routers.
    # Force-join guard must run before feature handlers so unjoined members cannot use commands either.
    router.include_router(force_join.router)
    # Check the active speed-type challenge before conversational FSM handlers
    # (deposit/withdraw/transfer/dice) can consume a member's one-word answer.
    # Non-matching messages raise SkipHandler and continue through normal routers.
    router.include_router(world_events.router)
    router.include_router(start.router)
    router.include_router(security.router)
    router.include_router(moderation.router)
    router.include_router(economy.router)
    router.include_router(games.router)
    router.include_router(missing_games.router)
    router.include_router(duels.router)
    router.include_router(statistics.router)
    router.include_router(settings.router)
    router.include_router(help.router)
    router.include_router(tournaments.router)
    router.include_router(affiliate.router)
    # Register this specific owner callback router before owner's catch-all callback.
    router.include_router(owner_group_message.router)
    # Group Activity owns the five-section owner submenu; register before legacy owner callbacks.
    router.include_router(owner_group_activity.router)
    # Group promotion requests must precede the owner's broad owner:* callback fallback.
    router.include_router(group_promotion.router)
    # Owner AI callback must precede owner's broad owner:* callback fallback.
    router.include_router(owner_ai_control.router)
    router.include_router(owner.router)
    router.include_router(alexa_games.router)
    router.include_router(group_tools.router)
    router.include_router(community_shield.router)
    router.include_router(scam_review.router)
    router.include_router(ultra_vip.router)
    router.include_router(ai.router)
    # Owner-only utility; intentionally omitted from Telegram's public command menu.
    router.include_router(sticker_id.router)
    # General casual AI chat runs after owner action handling.
    router.include_router(ai_chat.router)
    # Keep welcome/join-leave/captcha processing, but after feature handlers.
    router.include_router(welcome.router)
    return router
