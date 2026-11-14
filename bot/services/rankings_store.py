"""Isolated SQLite storage for per-group chat rankings and short-lived AI recap context."""
import asyncio
import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import aiosqlite

_TZ = ZoneInfo("Asia/Karachi")
_DB_PATH = Path(os.getenv("ALEXA_RANKINGS_DB_PATH", "./data/alexa_rankings.sqlite3"))
_LOCK = asyncio.Lock()
_READY = False


def local_now() -> datetime:
    return datetime.now(_TZ)


async def init_rankings_db() -> None:
    global _READY
    if _READY:
        return
    async with _LOCK:
        if _READY:
            return
        _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(_DB_PATH, timeout=20) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA busy_timeout=20000")
            await db.executescript("""
                CREATE TABLE IF NOT EXISTS ranking_totals (
                    group_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    username TEXT,
                    first_name TEXT NOT NULL,
                    message_count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (group_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS ranking_daily (
                    group_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    day_key TEXT NOT NULL,
                    username TEXT,
                    first_name TEXT NOT NULL,
                    message_count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (group_id, user_id, day_key)
                );
                CREATE INDEX IF NOT EXISTS ix_ranking_daily_group_day
                    ON ranking_daily(group_id, day_key);
                CREATE TABLE IF NOT EXISTS ranking_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    first_name TEXT NOT NULL,
                    body TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_ranking_messages_group_date
                    ON ranking_messages(group_id, created_at);
            """)
            await db.commit()
        _READY = True


async def record_group_message(message) -> None:
    """Count human group messages; retain text/captions for AI recap for 7 days only."""
    user = getattr(message, "from_user", None)
    chat = getattr(message, "chat", None)
    if not user or user.is_bot or not chat or chat.type not in {"group", "supergroup"}:
        return
    if (message.text or "").lstrip().startswith("/"):
        return
    has_content = any(getattr(message, field, None) for field in (
        "text", "caption", "photo", "document", "sticker", "video",
        "audio", "voice", "animation", "video_note", "contact", "location",
    ))
    if not has_content:
        return

    now = local_now()
    day_key = now.date().isoformat()
    body = (message.text or message.caption or "").strip()
    await init_rankings_db()
    async with _LOCK:
        async with aiosqlite.connect(_DB_PATH, timeout=20) as db:
            await db.execute("PRAGMA busy_timeout=20000")
            await db.execute("""
                INSERT INTO ranking_totals(group_id,user_id,username,first_name,message_count)
                VALUES(?,?,?,?,1)
                ON CONFLICT(group_id,user_id) DO UPDATE SET
                    username=excluded.username,
                    first_name=excluded.first_name,
                    message_count=ranking_totals.message_count+1
            """, (chat.id, user.id, user.username, user.first_name or "Member"))
            await db.execute("""
                INSERT INTO ranking_daily(group_id,user_id,day_key,username,first_name,message_count)
                VALUES(?,?,?,?,?,1)
                ON CONFLICT(group_id,user_id,day_key) DO UPDATE SET
                    username=excluded.username,
                    first_name=excluded.first_name,
                    message_count=ranking_daily.message_count+1
            """, (chat.id, user.id, day_key, user.username, user.first_name or "Member"))
            if body:
                await db.execute("""
                    INSERT INTO ranking_messages(group_id,user_id,first_name,body,created_at)
                    VALUES(?,?,?,?,?)
                """, (chat.id, user.id, user.first_name or "Member", body[:1200], now.isoformat()))
            # Keep message text only for the rolling weekly recap; all-time counts remain.
            cutoff = (now - timedelta(days=7)).isoformat()
            await db.execute("DELETE FROM ranking_messages WHERE created_at < ?", (cutoff,))
            await db.commit()


async def get_rankings(group_id: int, period: str):
    await init_rankings_db()
    now = local_now()
    async with aiosqlite.connect(_DB_PATH, timeout=20) as db:
        db.row_factory = aiosqlite.Row
        if period == "overall":
            query = """
                SELECT user_id,username,first_name,message_count
                FROM ranking_totals WHERE group_id=?
                ORDER BY message_count DESC, first_name COLLATE NOCASE ASC LIMIT 10
            """
            params = (group_id,)
            total_cursor = await db.execute(
                "SELECT COALESCE(SUM(message_count),0) AS total FROM ranking_totals WHERE group_id=?",
                (group_id,),
            )
            total_row = await total_cursor.fetchone()
        else:
            if period == "today":
                start_day = now.date().isoformat()
            else:
                start_day = (now.date() - timedelta(days=6)).isoformat()
            query = """
                SELECT user_id,MAX(username) AS username,MAX(first_name) AS first_name,
                       SUM(message_count) AS message_count
                FROM ranking_daily WHERE group_id=? AND day_key>=?
                GROUP BY user_id
                ORDER BY message_count DESC, first_name COLLATE NOCASE ASC LIMIT 10
            """
            params = (group_id, start_day)
            total_cursor = await db.execute(
                "SELECT COALESCE(SUM(message_count),0) AS total FROM ranking_daily WHERE group_id=? AND day_key>=?",
                (group_id, start_day),
            )
            total_row = await total_cursor.fetchone()
        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()
        return [dict(row) for row in rows], int(total_row["total"] or 0)


async def get_recap_messages(group_id: int, period: str, limit: int = 80):
    await init_rankings_db()
    now = local_now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week" or period == "overall":
        start = start - timedelta(days=6)
    async with aiosqlite.connect(_DB_PATH, timeout=20) as db:
        await db.execute(
            "DELETE FROM ranking_messages WHERE created_at < ?",
            ((now - timedelta(days=7)).isoformat(),),
        )
        await db.commit()
        cursor = await db.execute("""
            SELECT first_name,body FROM ranking_messages
            WHERE group_id=? AND created_at>=?
            ORDER BY id DESC LIMIT ?
        """, (group_id, start.isoformat(), limit))
        rows = await cursor.fetchall()
    return list(reversed(rows))
