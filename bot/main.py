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
    """Show public commands to members and full group commands to chat administrators."""
    from bot.config import settings
    from aiogram.types import (
        BotCommand, BotCommandScopeDefault, BotCommandScopeChat,
        BotCommandScopeAllPrivateChats, BotCommandScopeAllGroupChats,
        BotCommandScopeAllChatAdministrators,
    )

    public_commands = [
        ("start", "Start ALEXA"), ("menu", "Open main menu"), ("help", "Show commands"),
        ("language", "Choose language"), ("wallet", "View wallet"), ("bal", "View balance"),
        ("daily", "Claim daily coins"), ("weekly", "Claim weekly coins"), ("monthly", "Claim monthly coins"),
        ("terms", "Premium Stars payment terms"), ("referral", "Get referral link"), ("achievements", "View achievements"),
        ("games", "Open games"), ("dice", "Play dice"), ("rps", "Rock paper scissors"),
        ("quiz", "Play quiz"), ("wheel", "Spin the wheel"), ("roulette", "Play roulette"),
        ("cards", "Play cards"), ("mines", "Play mines"), ("treasure", "Treasure hunt"),
        ("numwar", "Number war"), ("stats", "View stats"), ("rank", "View your rank"),
        ("leaders", "Top players"), ("items", "View your items"), ("pfp", "Profile photo"),
        ("war", "Multiverse Wars menu"), ("warmission", "View daily war mission"),
        ("waranswer", "Answer daily war mission"), ("warboard", "Weekly war leaderboard"),
        ("warresult", "View a group challenge result"),
        ("worldrank", "All-time world group ranking"), ("mystery", "Open daily mystery reward"),
        ("empire", "Multiverse Empire ranking"),
        ("empirecontribute", "Contribute to your Empire"), ("vip", "View Elite Club status"),
        ("vault", "Open daily Legendary Vault"), ("badges", "View collectible badges"),
        ("arena", "Open World Arena"), ("play", "Play a World Arena challenge"),
        ("arenadaily", "Attempt daily Arena mission"), ("battle", "Challenge a player"),
        ("myrank", "View World Arena rank"), ("worldtop", "Global Arena leaderboard"),
        ("grouprank", "This group Arena leaderboard"), ("worldgroups", "Global group Arena ranking"),
    ]
    admin_commands = public_commands + [
        ("security", "Group security panel"), ("settings", "Group settings"),
        ("ban", "Ban a member"), ("unban", "Unban a member"), ("kick", "Kick a member"),
        ("mute", "Mute a member"), ("unmute", "Unmute a member"), ("promote", "Promote a member"),
        ("demote", "Demote a member"), ("warn", "Warn a member"), ("unwarn", "Remove warning"),
        ("warns", "View warnings"), ("warnings", "View warnings"), ("resetwarns", "Reset warnings"),
        ("del", "Delete a replied message"), ("pin", "Pin a message"), ("unpin", "Unpin a message"),
        ("history", "Moderation history"), ("setflood", "Configure flood limit"),
        ("flood", "Show flood settings"), ("setfloodmode", "Set flood action"),
        ("clearflood", "Clear flood counters"), ("setrole", "Set member label"),
        ("clearrole", "Clear member label"), ("role", "Manage member labels"),
        ("lock", "Lock group content"), ("unlock", "Unlock group content"),
        ("locks", "Open content locks"), ("locktypes", "Show lock types"),
        ("rules", "Show group rules"), ("setrules", "Set group rules"),
        ("filter", "Add keyword auto-reply"), ("stopfilter", "Remove keyword filter"),
        ("save", "Save a message"), ("get", "Get saved message"), ("clear", "Clear saved message"),
        ("blocklist", "Show blocked keywords"), ("addblocklist", "Block a keyword"),
        ("rmblocklist", "Unblock a keyword"), ("disablecmd", "Disable group command"),
        ("enablecmd", "Enable group command"), ("disabledcmds", "List disabled commands"),
        ("id", "Show IDs"), ("info", "Show user info"), ("admins", "List group admins"),
        ("report", "Report a member"), ("grouppromo", "Group promotion tools"),
        ("deposit", "Deposit coins"), ("withdraw", "Withdraw coins"), ("transfer", "Transfer coins"),
        ("toprich", "Richest players"), ("topkill", "Top virtual kill scores"),
        ("toprob", "Top successful robs"), ("protect", "Buy virtual protection"),
        ("unprotect", "Remove protection"), ("kill", "Virtual game action"),
        ("rob", "Virtual coin game"), ("revive", "Revive game profile"),
        ("bluff", "Play bluff game"), ("hack", "Play hack game"), ("task", "View tasks"),
        ("shield", "Configure Community Shield"), ("shieldreport", "Report suspected scam"),
        ("shieldcheck", "Check Community Shield reports"),
        ("scamreview", "Queue a scam review"), ("scamreviews", "List pending scam reviews"),
        ("scamresolve", "Mark a scam review as reviewed"),
        ("empirecreate", "Create this group Empire"),
        ("warregister", "Register this group for Wars"), ("warleave", "Leave Multiverse Wars"),
        ("warchallenge", "Challenge another registered group"), ("waraccept", "Accept a group war challenge"),
    ]
    public = [BotCommand(command=name, description=description) for name, description in public_commands]
    admin = [BotCommand(command=name, description=description) for name, description in admin_commands]
    try:
        # Normal members see only public commands. Telegram automatically applies
        # the administrator scope to group admins and the group creator.
        await bot.set_my_commands(public, scope=BotCommandScopeDefault())
        await bot.set_my_commands(public, scope=BotCommandScopeAllPrivateChats())
        await bot.set_my_commands(public, scope=BotCommandScopeAllGroupChats())
        await bot.set_my_commands(admin, scope=BotCommandScopeAllChatAdministrators())
        owner_id = settings.effective_owner_id
        if owner_id:
            owner_commands = admin + [BotCommand(command="owner", description="Open ALEXA Owner Panel")]
            await bot.set_my_commands(owner_commands, scope=BotCommandScopeChat(chat_id=owner_id))
        logger.info("Telegram command visibility configured", owner_id=settings.effective_owner_id or None)
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
            allowed_updates=['message', 'callback_query', 'pre_checkout_query', 'chat_member', 'my_chat_member'],
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
