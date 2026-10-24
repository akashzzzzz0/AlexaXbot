import asyncio
import os
import traceback
import structlog
from aiohttp import web

logger = structlog.get_logger()
_last_error = None  # stores last crash reason, shown by /health


async def health_handler(request):
    global _last_error
    if _last_error:
        return web.Response(
            text=f'DEGRADED: {_last_error}',
            status=200,  # still 200 so Render doesn't restart
            content_type='text/plain',
        )
    return web.Response(text='OK', status=200)



async def run_health_server():
    port = int(os.environ.get('PORT', 8080))
    app = web.Application()
    app.router.add_get('/health', health_handler)
    app.router.add_get('/', health_handler)

    try:
        from bot.admin_api import setup_admin_routes
        setup_admin_routes(app)
    except Exception as e:
        logger.warning('Admin routes not loaded', error=str(e))

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port, reuse_port=True)
    try:
        await site.start()
        logger.info('Health check server started', port=port)
        return runner
    except OSError as exc:
        # On PythonAnywhere a previous console process may still own this port.
        # A health endpoint is optional; do not prevent Telegram polling from starting.
        logger.warning('Health server unavailable; continuing without it', port=port, error=str(exc))
        await runner.cleanup()
        return None


async def setup_database_with_retry(retries=5, delay=3):
    for attempt in range(1, retries + 1):
        try:
            from bot.database.connection import create_tables
            await create_tables()
            logger.info('Database tables created successfully')
            return True
        except BaseException as e:
            logger.warning(f'DB setup attempt {attempt}/{retries} failed', error=str(e))
            if attempt < retries:
                await asyncio.sleep(delay)
    logger.warning('Configured database unreachable; trying persistent local SQLite fallback')
    try:
        from bot.database.connection import switch_to_local_sqlite
        await switch_to_local_sqlite()
        logger.info('Local SQLite database ready')
        return True
    except Exception as fallback_error:
        logger.error('Local SQLite fallback failed', error=str(fallback_error))
        return False



async def configure_command_visibility(bot) -> None:
    """Keep game commands public; show full controls to group admins and owner in DM."""
    from bot.config import settings
    from aiogram.types import (
        BotCommand, BotCommandScopeDefault, BotCommandScopeChat,
        BotCommandScopeAllPrivateChats, BotCommandScopeAllGroupChats,
        BotCommandScopeAllChatAdministrators,
    )

    # Normal members get game/everyday play commands only. Help can still show
    # the complete command guide in private chat without exposing admin controls
    # in the Telegram slash-command menu.
    public_commands = [
        ("start", "Start ALEXA"), ("help", "Open full command guide"),
        ("menu", "Open main menu"), ("games", "Open games"),
        ("dice", "Play dice"), ("rps", "Play Rock Paper Scissors"),
        ("quiz", "Play quiz"), ("wheel", "Spin the wheel"),
        ("numwar", "Play Number War"), ("cards", "Play card game"),
        ("card", "Play card game"),
        ("treasure", "Play treasure hunt"), ("mines", "Play mines"),
        ("roulette", "Play roulette"), ("rullet", "Play roulette"),
        ("ludo", "Play Ludo race"), ("bingo", "Play Bingo"),
        ("duel", "Challenge a player"),
        ("bal", "View game balance"), ("kill", "Virtual game action"),
        ("rob", "Try a virtual robbery"), ("revive", "Revive game profile"),
        ("protect", "Buy virtual protection"), ("unprotect", "Remove protection"),
        ("rank", "View your rank"), ("leaders", "View player leaderboard"),
        ("topkill", "Top virtual kill scores"), ("toprich", "Richest players"),
        ("toprob", "Top successful robs"), ("items", "View game items"),
        ("task", "View game tasks"), ("bomb", "Play Bomb"), ("bluff", "Play Bluff"),
        ("hack", "Play Hack"), ("pfp", "View profile photo"),
        ("empire", "View Empire ranking"), ("empirecontribute", "Contribute Empire XP"),
        ("vip", "View Elite Club status"), ("pro", "View VIP status"),
        ("vault", "Open Legendary Vault"), ("badges", "View badges"),
    ]

    admin_commands = public_commands + [
        ("language", "Choose language"), ("lang", "Choose language"),
        ("wallet", "View wallet"), ("balance", "View balance"),
        ("economy", "View economy"), ("eco", "View economy"),
        ("daily", "Claim daily coins"), ("weekly", "Claim weekly coins"),
        ("monthly", "Claim monthly coins"), ("deposit", "Deposit coins"),
        ("withdraw", "Withdraw coins"), ("transfer", "Transfer coins"),
        ("referral", "Get referral link"), ("ref", "Get referral link"),
        ("achievements", "View achievements"), ("transactions", "View transactions"),
        ("tx", "View transactions"),
        ("stats", "View statistics"), ("gstats", "View group statistics"),
        ("security", "Group security panel"), ("settings", "Group settings"),
        ("setwelcome", "Set group welcome message"), ("ban", "Ban a member"),
        ("unban", "Unban a member"), ("kick", "Kick a member"),
        ("mute", "Mute a member"), ("unmute", "Unmute a member"),
        ("promote", "Promote a member"), ("demote", "Demote a member"),
        ("warn", "Warn a member"), ("unwarn", "Remove warning"),
        ("warns", "View warnings"), ("warnings", "View warnings"),
        ("resetwarn", "Reset warnings"), ("resetwarns", "Reset warnings"),
        ("approvebio", "Approve bio link"), ("note", "Add a member note"),
        ("del", "Delete a replied message"), ("purg", "Bulk-delete messages"),
        ("pin", "Pin a message"), ("unpin", "Unpin a message"),
        ("history", "View moderation history"), ("setflood", "Configure flood limit"),
        ("flood", "Show flood settings"), ("setfloodmode", "Set flood action"),
        ("clearflood", "Clear flood counters"), ("setrole", "Set member label"),
        ("clearrole", "Clear member label"), ("role", "Manage member labels"),
        ("lock", "Lock group content"), ("unlock", "Unlock group content"),
        ("locks", "List content locks"), ("locktypes", "List lock types"),
        ("rules", "Show group rules"), ("setrules", "Set group rules"),
        ("filter", "Add keyword auto-reply"), ("stopfilter", "Remove keyword filter"),
        ("save", "Save a message"), ("get", "Get saved message"),
        ("clear", "Clear saved message"), ("blocklist", "Show blocked words"),
        ("addblocklist", "Block a word"), ("rmblocklist", "Unblock a word"),
        ("disablecmd", "Disable a group command"), ("enablecmd", "Enable a command"),
        ("disabledcmds", "List disabled commands"), ("id", "Show IDs"),
        ("info", "Show user info"), ("admins", "List group admins"),
        ("report", "Report a member"), ("tagall", "Mention all saved group members"), ("admin", "Mention group admins"), ("autodelete", "Configure auto-delete"),
        ("grouppromo", "Group promotion tools"), ("shield", "Community Shield settings"),
        ("shieldreport", "Report suspected scam"), ("shieldcheck", "Check scam reports"),
        ("scamreview", "Queue scam review"), ("scamreviews", "List scam reviews"),
        ("scamresolve", "Resolve scam review"), ("approve", "Approve a member"),
        ("unapprove", "Remove member approval"), ("empirecreate", "Create group Empire"),
    ]

    # BotFather/Telegram accepts at most 100 commands in each scope. Keep every
    # distinct command, and fail clearly in logs if a future addition exceeds it.
    def to_bot_commands(items):
        unique = {}
        for name, description in items:
            unique[name] = description
        commands = list(unique.items())
        if len(commands) > 100:
            logger.warning(
                "Telegram command menu exceeds the 100-command limit; "
                "remaining commands will still be available in /help",
                total_commands=len(commands),
                shown_commands=100,
            )
            commands = commands[:100]
        return [BotCommand(command=name, description=description) for name, description in commands]

    public = to_bot_commands(public_commands)
    # Telegram hard-limits each slash-command menu to 100 commands. Prioritize
    # the most-used economy and group-admin controls, then include game commands.
    # The complete command catalogue remains available through /help.
    admin_priority = [
        ("wallet", "View wallet"), ("daily", "Claim daily coins"),
        ("weekly", "Claim weekly coins"), ("monthly", "Claim monthly coins"),
        ("transfer", "Transfer coins"), ("referral", "Get referral link"),
        ("achievements", "View achievements"), ("security", "Group security panel"),
        ("settings", "Group settings"), ("ban", "Ban a member"),
        ("unban", "Unban a member"), ("kick", "Kick a member"),
        ("mute", "Mute a member"), ("unmute", "Unmute a member"),
        ("promote", "Promote a member"), ("demote", "Demote a member"),
        ("warn", "Warn a member"), ("unwarn", "Remove warning"),
        ("warns", "View warnings"), ("resetwarns", "Reset warnings"),
        ("del", "Delete a replied message"), ("purg", "Bulk-delete messages"),
        ("pin", "Pin a message"), ("unpin", "Unpin a message"),
        ("history", "Moderation history"), ("tagall", "Mention all saved members"), ("admin", "Mention group admins"), ("lock", "Lock group content"),
        ("unlock", "Unlock group content"), ("locks", "List content locks"),
        ("locktypes", "List lock types"), ("setflood", "Configure flood limit"),
        ("flood", "Show flood settings"), ("setfloodmode", "Set flood action"),
        ("clearflood", "Clear flood counters"), ("rules", "Show group rules"),
        ("setrules", "Set group rules"), ("id", "Show IDs"),
        ("info", "Show user info"), ("admins", "List group admins"),
        ("report", "Report a member"),
    ]
    admin = to_bot_commands(admin_priority + admin_commands)
    try:
        # Public/default, private chats and group chats all get the games menu.
        await bot.set_my_commands(public, scope=BotCommandScopeDefault())
        await bot.set_my_commands(public, scope=BotCommandScopeAllPrivateChats())
        await bot.set_my_commands(public, scope=BotCommandScopeAllGroupChats())
        # Telegram automatically applies this scope to group admins and creators.
        await bot.set_my_commands(admin, scope=BotCommandScopeAllChatAdministrators())
        owner_id = settings.effective_owner_id
        if owner_id:
            # The owner's private chat gets the full command menu; /owner remains hidden.
            await bot.set_my_commands(admin, scope=BotCommandScopeChat(chat_id=owner_id))
        logger.info(
            "Telegram command visibility configured",
            public_command_count=len(public),
            admin_command_count=len(admin),
            owner_id=settings.effective_owner_id or None,
        )
    except Exception as exc:
        logger.warning("Could not configure command visibility", error=str(exc))


async def on_startup(bot) -> None:
    from bot.config import settings
    logger.info('Bot starting up', environment=settings.ENVIRONMENT)
    if not settings.effective_owner_id:
        logger.warning('BOT_OWNER_ID/OWNER_ID is not configured; the owner-only panel will be unavailable until a numeric owner ID is set')

    try:
        from bot.middlewares import load_translations
        load_translations()
        logger.info('Translations loaded')
    except Exception as e:
        logger.error('Failed to load translations', error=str(e))

    db_ok = await setup_database_with_retry()

    if db_ok:
        try:
            from bot.database.connection import async_session_maker
            from bot.services.achievement_service import seed_achievements
            from bot.services.mission_service import seed_missions
            async with async_session_maker() as session:
                await seed_achievements(session)
                await seed_missions(session)
                await session.commit()
            logger.info('Achievements and missions seeded')
        except Exception as e:
            logger.warning('Could not seed achievements', error=str(e))

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await configure_command_visibility(bot)
        logger.info('Polling mode — webhook cleared')
    except Exception as e:
        logger.error('Failed to clear webhook', error=str(e))


    logger.info('Bot startup complete', db_ready=db_ok)

    # Notify the configured owner privately whenever ALEXA starts successfully.
    owner_id = settings.effective_owner_id
    if owner_id:
        try:
            me = await bot.get_me()
            await bot.send_message(
                owner_id,
                "🤖 <b>ALEXA Bot Is Active Now!</b>\n\n"
                f"✅ Bot: @{me.username or me.first_name}\n"
                "🟢 Startup completed and the bot is ready to run."
            )
            logger.info("Owner notified that bot is active", owner_id=owner_id)
        except Exception as exc:
            # A missing /start or blocked bot must not stop group operation.
            logger.warning("Could not send startup DM to owner", owner_id=owner_id, error=str(exc))


async def on_shutdown(bot, runner) -> None:
    logger.info('Bot shutting down')
    try:
        from bot.database.connection import close_db
        await close_db()
    except Exception:
        pass
    try:
        await runner.cleanup()
    except Exception:
        pass
    logger.info('Shutdown complete')


async def run_bot(bot, dp, runner):
    """Run the bot polling loop. Returns when polling ends."""
    global _last_error
    try:
        await on_startup(bot)
    except BaseException as e:
        tb = traceback.format_exc()
        _last_error = f'on_startup failed: {e}\n{tb}'
        logger.error('Bot startup failed', error=str(e), traceback=tb)
        return

    logger.info('Starting polling')
    try:
        await dp.start_polling(
            bot,
            allowed_updates=['message', 'callback_query', 'inline_query', 'chosen_inline_result', 'pre_checkout_query', 'chat_member', 'my_chat_member'],
        )
    except asyncio.CancelledError:
        pass
    except BaseException as e:
        tb = traceback.format_exc()
        _last_error = f'Polling crashed: {e}\n{tb}'
        logger.error('Polling crashed', error=str(e), traceback=tb)

    await on_shutdown(bot, runner)


async def main() -> None:
    global _last_error
    try:
        runner = await run_health_server()
    except Exception as exc:
        logger.warning('Health server failed; continuing without it', error=str(exc))
        runner = None

    try:
        from bot.utils.logger import setup_logging
        setup_logging()
    except Exception as e:
        logger.warning('Could not set up structured logging', error=str(e))

    try:
        from bot.config import settings
    except BaseException as e:
        tb = traceback.format_exc()
        _last_error = f'Config load failed: {e}\n{tb}'
        logger.error('Failed to load config', error=str(e), traceback=tb)
        await asyncio.Event().wait()
        return

    if not settings.is_configured:
        _last_error = 'BOT_TOKEN is missing'
        logger.error(
            'BOT_TOKEN is not set. '
            'Please set BOT_TOKEN in your host environment or project .env file. '
            'The health server is running but the bot is NOT active.'
        )
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass
        await runner.cleanup()
        return

    if settings.SENTRY_DSN:
        try:
            import sentry_sdk
            sentry_sdk.init(dsn=settings.SENTRY_DSN, environment=settings.ENVIRONMENT)
        except Exception as e:
            logger.warning('Sentry init failed', error=str(e))

    try:
        from aiogram import Bot, Dispatcher
        from aiogram.client.session.aiohttp import AiohttpSession
        from aiogram.client.default import DefaultBotProperties
        from aiogram.enums import ParseMode
        from aiogram.fsm.storage.memory import MemoryStorage
        from bot.handlers import get_main_router
        from bot.middlewares import DatabaseMiddleware, I18nMiddleware, CallbackLoggingMiddleware
    except BaseException as e:
        tb = traceback.format_exc()
        _last_error = f'Import failed: {e}\n{tb}'
        logger.error('Critical import failed', error=str(e), traceback=tb)
        await asyncio.Event().wait()
        return

    storage = MemoryStorage()
    # PythonAnywhere blocks direct outbound TCP; route Telegram HTTPS through its proxy when available.
    proxy_url = (os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY") or
                 os.environ.get("http_proxy") or os.environ.get("HTTP_PROXY"))
    session = AiohttpSession(proxy=proxy_url) if proxy_url else AiohttpSession()
    bot = Bot(
        token=settings.BOT_TOKEN,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=storage)
    dp.include_router(get_main_router())
    from bot.middlewares.auto_delete import AutoDeleteCommandMiddleware
    dp.message.outer_middleware(AutoDeleteCommandMiddleware())

    # IMPORTANT: Middleware registration order in aiogram 3.
    # outer_middleware uses LIFO — last registered runs FIRST.
    # Desired: CallbackLogging → Database → I18n → handler
    # Register in reverse: I18n first, Database second, CallbackLogging last.
    dp.update.outer_middleware(I18nMiddleware())
    dp.update.outer_middleware(DatabaseMiddleware())
    dp.update.outer_middleware(CallbackLoggingMiddleware())

    # Run bot (handles own errors, never raises)
    await run_bot(bot, dp, runner)

    # If bot stops, keep the health server alive for host diagnostics
    if _last_error:
        logger.error('Bot stopped with error — keeping health server alive for diagnostics')
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info('Bot stopped')
    except BaseException as e:
        logger.error('Unexpected fatal error', error=str(e), traceback=traceback.format_exc())
        raise
