import ssl
import structlog
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from typing import AsyncGenerator, Optional
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from bot.config import settings

logger = structlog.get_logger()

# Module-level singletons (lazily initialized)
_engine = None
_session_maker = None


def _prepare_db_url_and_ssl(url: str):
    """Strip PostgreSQL-only options and return (clean_url, connect_args)."""
    # urlunparse collapses the triple slash in relative SQLite URLs; leave them untouched.
    if url.startswith("sqlite+"):
        return url, {}
    parsed = urlparse(url)
    params = parse_qs(parsed.query, keep_blank_values=True)

    ssl_ctx = None
    sslmode = params.pop("sslmode", [None])[0]
    # asyncpg does not accept libpq-only channel_binding as a connect kwarg.
    params.pop("channel_binding", None)
    if sslmode in ("require", "verify-full"):
        # Verify the server certificate using the host's trusted CA store.
        ssl_ctx = ssl.create_default_context()
    elif sslmode == "verify-ca":
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
    elif sslmode == "disable":
        ssl_ctx = False

    new_query = urlencode(params, doseq=True)
    clean = urlunparse(parsed._replace(query=new_query))
    connect_args = {"ssl": ssl_ctx} if ssl_ctx is not None else {"ssl": False}
    return clean, connect_args


def _init_engine():
    """Create the async engine and session maker if not already done."""
    global _engine, _session_maker
    if _engine is not None:
        return

    db_url = settings.DATABASE_URL
    if not db_url:
        raise RuntimeError("DATABASE_URL is not configured")

    clean_url, connect_args = _prepare_db_url_and_ssl(db_url)
    if clean_url.startswith("sqlite+"):
        # SQLite's busy timeout is in seconds for the driver. Give other
        # transactions time to finish instead of failing immediately under load.
        connect_args = {"timeout": 30}

    engine_options = {
        "echo": False,
        "connect_args": connect_args,
    }
    if clean_url.startswith("sqlite+"):
        # Serialize local SQLite transactions through one pooled connection.
        engine_options.update({"pool_size": 1, "max_overflow": 0})
    # SQLite uses a different pool implementation and rejects PostgreSQL pool
    # sizing arguments. Only pass these options to network databases.
    if not clean_url.startswith("sqlite+"):
        engine_options.update({
            "pool_pre_ping": True,
            "pool_size": settings.POOL_SIZE,
            "max_overflow": max(0, settings.MAX_CONNECTIONS - settings.POOL_SIZE),
        })
    _engine = create_async_engine(clean_url, **engine_options)
    if clean_url.startswith("sqlite+"):
        # Keep SQLite responsive under concurrent Telegram updates. WAL improves
        # reader/writer coexistence; remaining pragmas reduce avoidable I/O.
        @event.listens_for(_engine.sync_engine, "connect")
        def _configure_sqlite(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA busy_timeout=30000")
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.execute("PRAGMA temp_store=MEMORY")
                cursor.execute("PRAGMA cache_size=-20000")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA journal_mode=WAL")
            finally:
                cursor.close()
    _session_maker = async_sessionmaker(
        _engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
        autocommit=False,
    )
    safe_url = clean_url.split("@")[-1] if "@" in clean_url else "***"
    logger.info("Database engine created", url=safe_url)


# Backward-compatible module-level attributes
# These are accessed as `from bot.database.connection import engine, async_session_maker`
# We use module __getattr__ to lazily initialize on first access


def __getattr__(name):
    if name == "engine":
        _init_engine()
        return _engine
    if name == "async_session_maker":
        _init_engine()
        return _session_maker
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    _init_engine()
    async with _session_maker() as session:
        yield session


async def create_tables() -> None:
    from bot.database.models import Base
    # Register Force Join model before create_all creates the schema.
    from bot.database import force_join as _force_join_models  # noqa: F401
    _init_engine()
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # create_all creates new tables but does not add columns to an existing deployment.
        from sqlalchemy import text
        if conn.dialect.name == "postgresql":
            statements = [
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS dm_started BOOLEAN NOT NULL DEFAULT FALSE",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS referral_link_requested BOOLEAN NOT NULL DEFAULT FALSE",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS has_added_bot_to_group BOOLEAN NOT NULL DEFAULT FALSE",
                "ALTER TABLE groups ADD COLUMN IF NOT EXISTS invite_link VARCHAR(512)",
                "ALTER TABLE group_settings ADD COLUMN IF NOT EXISTS auto_delete_seconds INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE group_settings ADD COLUMN IF NOT EXISTS silent_service_messages BOOLEAN NOT NULL DEFAULT FALSE",
                "ALTER TABLE group_members ADD COLUMN IF NOT EXISTS approved_sticker BOOLEAN NOT NULL DEFAULT FALSE",
                "ALTER TABLE group_filters ADD COLUMN IF NOT EXISTS response_type VARCHAR(16) NOT NULL DEFAULT 'text'",
                "ALTER TABLE group_filters ADD COLUMN IF NOT EXISTS target_user_id BIGINT",
                "ALTER TABLE group_filters ADD COLUMN IF NOT EXISTS target_username VARCHAR(64)",
            ]
            for statement in statements:
                await conn.execute(text(statement))
        elif conn.dialect.name == "sqlite":
            for table, column, definition in [
                ("users", "dm_started", "BOOLEAN NOT NULL DEFAULT 0"),
                ("users", "referral_link_requested", "BOOLEAN NOT NULL DEFAULT 0"),
                ("users", "has_added_bot_to_group", "BOOLEAN NOT NULL DEFAULT 0"),
                ("groups", "invite_link", "VARCHAR(512)"),
                ("group_settings", "auto_delete_seconds", "INTEGER NOT NULL DEFAULT 0"),
                ("group_settings", "silent_service_messages", "BOOLEAN NOT NULL DEFAULT 0"),
                ("group_members", "approved_sticker", "BOOLEAN NOT NULL DEFAULT 0"),
                ("group_filters", "response_type", "VARCHAR(16) NOT NULL DEFAULT 'text'"),
                ("group_filters", "target_user_id", "BIGINT"),
                ("group_filters", "target_username", "VARCHAR(64)"),
            ]:
                cols = await conn.exec_driver_sql(f"PRAGMA table_info({table})")
                if column not in {row[1] for row in cols.fetchall()}:
                    await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {definition}"))

        # Add partial unique indexes to existing databases as create_all does not
        # retrofit indexes onto tables that were already created. Keep the newest
        # open record and close older duplicate records before enforcing uniqueness.
        if conn.dialect.name == "sqlite":
            await conn.execute(text("""
                UPDATE affiliate_requests
                SET status = 'cancelled'
                WHERE status IN ('pending', 'approved')
                  AND id NOT IN (
                    SELECT MAX(id) FROM affiliate_requests
                    WHERE status IN ('pending', 'approved')
                    GROUP BY user_id, request_type
                  )
            """))
            await conn.execute(text("""
                UPDATE group_promotion_requests
                SET status = 'cancelled'
                WHERE status IN ('pending', 'approved')
                  AND id NOT IN (
                    SELECT MAX(id) FROM group_promotion_requests
                    WHERE status IN ('pending', 'approved')
                    GROUP BY group_id
                  )
            """))
            await conn.execute(text("""
                UPDATE speed_type_events
                SET status = 'cancelled'
                WHERE status = 'active'
                  AND id NOT IN (
                    SELECT MAX(id) FROM speed_type_events
                    WHERE status = 'active'
                    GROUP BY chat_id
                  )
            """))
            await conn.execute(text("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_affiliate_requests_open_user_type
                ON affiliate_requests (user_id, request_type)
                WHERE status IN ('pending', 'approved')
            """))
            await conn.execute(text("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_group_promotion_open_group
                ON group_promotion_requests (group_id)
                WHERE status IN ('pending', 'approved')
            """))
            await conn.execute(text("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_speed_type_one_active_event_per_chat
                ON speed_type_events (chat_id)
                WHERE status = 'active'
            """))
        elif conn.dialect.name == "postgresql":
            await conn.execute(text("""
                WITH ranked AS (
                    SELECT id, ROW_NUMBER() OVER (
                        PARTITION BY user_id, request_type ORDER BY id DESC
                    ) AS rn
                    FROM affiliate_requests
                    WHERE status IN ('pending', 'approved')
                )
                UPDATE affiliate_requests AS target
                SET status = 'cancelled'
                FROM ranked
                WHERE target.id = ranked.id AND ranked.rn > 1
            """))
            await conn.execute(text("""
                WITH ranked AS (
                    SELECT id, ROW_NUMBER() OVER (
                        PARTITION BY group_id ORDER BY id DESC
                    ) AS rn
                    FROM group_promotion_requests
                    WHERE status IN ('pending', 'approved')
                )
                UPDATE group_promotion_requests AS target
                SET status = 'cancelled'
                FROM ranked
                WHERE target.id = ranked.id AND ranked.rn > 1
            """))
            await conn.execute(text("""
                WITH ranked AS (
                    SELECT id, ROW_NUMBER() OVER (
                        PARTITION BY chat_id ORDER BY id DESC
                    ) AS rn
                    FROM speed_type_events
                    WHERE status = 'active'
                )
                UPDATE speed_type_events AS target
                SET status = 'cancelled'
                FROM ranked
                WHERE target.id = ranked.id AND ranked.rn > 1
            """))
            await conn.execute(text("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_affiliate_requests_open_user_type
                ON affiliate_requests (user_id, request_type)
                WHERE status IN ('pending', 'approved')
            """))
            await conn.execute(text("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_group_promotion_open_group
                ON group_promotion_requests (group_id)
                WHERE status IN ('pending', 'approved')
            """))
            await conn.execute(text("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_speed_type_one_active_event_per_chat
                ON speed_type_events (chat_id)
                WHERE status = 'active'
            """))

        # Retrofit the active-CAPTCHA uniqueness rule on existing databases.
        if conn.dialect.name == "sqlite":
            await conn.execute(text("""
                UPDATE captcha_challenges
                SET is_solved = 1
                WHERE is_solved = 0
                  AND id NOT IN (
                    SELECT MAX(id) FROM captcha_challenges
                    WHERE is_solved = 0
                    GROUP BY group_id, user_id
                  )
            """))
            await conn.execute(text("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_captcha_one_unsolved_per_member_group
                ON captcha_challenges (group_id, user_id)
                WHERE is_solved = 0
            """))
        elif conn.dialect.name == "postgresql":
            await conn.execute(text("""
                WITH ranked AS (
                    SELECT id, ROW_NUMBER() OVER (
                        PARTITION BY group_id, user_id ORDER BY id DESC
                    ) AS rn
                    FROM captcha_challenges
                    WHERE is_solved = FALSE
                )
                UPDATE captcha_challenges AS target
                SET is_solved = TRUE
                FROM ranked
                WHERE target.id = ranked.id AND ranked.rn > 1
            """))
            await conn.execute(text("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_captcha_one_unsolved_per_member_group
                ON captcha_challenges (group_id, user_id)
                WHERE is_solved = FALSE
            """))

    logger.info("Database tables ensured")


async def close_db() -> None:
    global _engine, _session_maker
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_maker = None
        logger.info("Database connection pool closed")


async def switch_to_local_sqlite() -> None:
    """Switch to a persistent local SQLite database when hosted PostgreSQL is unreachable."""
    global _engine, _session_maker
    if _engine is not None:
        try:
            await _engine.dispose()
        except Exception:
            pass
    _engine = None
    _session_maker = None
    settings.DATABASE_URL = "sqlite+aiosqlite:///./alexa.db"
    logger.warning("Switching to local SQLite database fallback")
    await create_tables()
