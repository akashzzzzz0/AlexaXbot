from aiogram import Router
from bot.handlers import start, security, moderation, economy, games, duels, statistics, settings, help, owner, welcome, tournaments, alexa_games, group_tools
from bot.handlers.auto_delete_middleware import StartMenuAutoDeleteMessageMiddleware, StartMenuAutoDeleteCallbackMiddleware


def get_main_router() -> Router:
    router = Router()
    router.message.outer_middleware(StartMenuAutoDeleteMessageMiddleware())
    router.callback_query.outer_middleware(StartMenuAutoDeleteCallbackMiddleware())
    # Suppress join/leave/ban service messages before other handlers can consume them.
    router.include_router(welcome.router)
    router.include_router(start.router)
    router.include_router(security.router)
    router.include_router(moderation.router)
    router.include_router(economy.router)
    router.include_router(games.router)
    router.include_router(duels.router)
    router.include_router(statistics.router)
    router.include_router(settings.router)
    router.include_router(help.router)
    router.include_router(tournaments.router)
    router.include_router(owner.router)
    router.include_router(alexa_games.router)
    router.include_router(group_tools.router)
    return router
