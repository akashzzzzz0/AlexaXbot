from aiogram import Router
from bot.handlers import start, security, moderation, economy, games, duels, statistics, settings, help, owner, welcome, tournaments, alexa_games, group_tools
from bot.handlers.auto_delete_middleware import AutoDeleteCommandMiddleware, StartMenuAutoDeleteCallbackMiddleware


def get_main_router() -> Router:
    router = Router()
    router.message.outer_middleware(AutoDeleteCommandMiddleware())
    router.callback_query.outer_middleware(StartMenuAutoDeleteCallbackMiddleware())
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
    # Keep the catch-all group-message observer last so it cannot swallow commands/FSM input.
    router.include_router(welcome.router)
    return router
