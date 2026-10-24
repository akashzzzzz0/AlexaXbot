from aiogram import Router
from bot.handlers import start, security, moderation, economy, games, duels, statistics, settings, help, affiliate, owner, owner_group_message, force_join, welcome, tournaments, alexa_games, group_tools, community_shield, scam_review, ultra_vip, ai, ai_chat, missing_games
from bot.handlers.auto_delete_middleware import StartMenuAutoDeleteMessageMiddleware, StartMenuAutoDeleteCallbackMiddleware


def get_main_router() -> Router:
    router = Router()
    router.message.outer_middleware(StartMenuAutoDeleteMessageMiddleware())
    router.callback_query.outer_middleware(StartMenuAutoDeleteCallbackMiddleware())
    # Register command and feature routers before welcome's broad group-message
    # fallback. The fallback belongs last so it cannot intercept group commands
    # or callbacks intended for the other feature routers.
    # Force-join guard must run before feature handlers so unjoined members cannot use commands either.
    router.include_router(force_join.router)
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
    router.include_router(owner.router)
    router.include_router(alexa_games.router)
    router.include_router(group_tools.router)
    router.include_router(community_shield.router)
    router.include_router(scam_review.router)
    router.include_router(ultra_vip.router)
    router.include_router(ai.router)
    router.include_router(ai_chat.router)
    # Keep welcome/join-leave/captcha processing, but after feature handlers.
    router.include_router(welcome.router)
    return router
