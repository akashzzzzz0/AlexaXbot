import json
import os
import hmac
import hashlib
import time
from pathlib import Path
from urllib.parse import parse_qsl
from datetime import datetime, timedelta, timezone
from aiohttp import web
from sqlalchemy import select, func, and_, or_, desc, cast, String
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models import User, Group, GroupMember, Wallet, Economy, ActionLog, SecuritySettings, Referral, AffiliateRequest, InboxMessage, GroupInboxMessage, GroupPromotionRequest, GlobalSetting

ADMIN_KEY = os.environ.get("ADMIN_KEY", "")

# Lazy accessor — avoids importing async_session_maker at module-load time
# so the bot can start even when DATABASE_URL is not yet available.
def __getattr__(name):
    if name == "async_session_maker":
        from bot.database.connection import async_session_maker as _sm
        return _sm
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def require_admin(handler):
    async def wrapper(request):
        key = request.headers.get("X-Admin-Key", "")
        init_data = request.headers.get("X-Telegram-Init-Data", "")
        owner_session = bool(init_data and _verify_owner_telegram_init_data(init_data))
        admin_session = bool(ADMIN_KEY and hmac.compare_digest(key, ADMIN_KEY))
        if not (owner_session or admin_session):
            return web.json_response({"error": "Unauthorized"}, status=401, headers={"Cache-Control": "no-store"})
        return await handler(request)
    wrapper.__name__ = handler.__name__
    return wrapper


def _user_to_dict(user, wallet=None, economy=None):
    return {
        "id": str(user.id),
        "username": user.username,
        "firstName": user.first_name,
        "lastName": user.last_name,
        "isBanned": user.is_banned,
        "isPremium": user.is_premium,
        "language": user.language,
        "balance": wallet.balance if wallet else None,
        "xp": economy.total_xp if economy else None,
        "level": economy.level if economy else None,
        "createdAt": user.created_at.isoformat() if user.created_at else None,
        "lastSeen": user.last_seen.isoformat() if user.last_seen else None,
    }


def _group_to_dict(group):
    return {
        "id": str(group.id),
        "title": group.title,
        "username": group.username,
        "type": group.type,
        "isActive": group.is_active,
        "memberCount": group.member_count,
        "language": group.language,
        "createdAt": group.created_at.isoformat() if group.created_at else None,
    }


@require_admin
async def stats_handler(request):
    try:
        async with async_session_maker() as session:
            today = datetime.utcnow() - timedelta(hours=24)

            total_users = await session.scalar(select(func.count()).select_from(User))
            active_today = await session.scalar(
                select(func.count()).select_from(User).where(User.last_seen >= today)
            )
            total_groups = await session.scalar(select(func.count()).select_from(Group))
            banned_users = await session.scalar(
                select(func.count()).select_from(User).where(User.is_banned == True)
            )
            total_messages = await session.scalar(
                select(func.sum(GroupMember.message_count)).select_from(GroupMember)
            ) or 0
            economy_total = await session.scalar(
                select(func.sum(Wallet.balance)).select_from(Wallet)
            ) or 0
            games_played = await session.scalar(
                select(func.count()).select_from(ActionLog).where(ActionLog.action.like("game_%"))
            ) or 0

            return web.json_response({
                "totalUsers": total_users or 0,
                "activeToday": active_today or 0,
                "totalGroups": total_groups or 0,
                "totalMessages": int(total_messages),
                "bannedUsers": banned_users or 0,
                "economyTotal": int(economy_total),
                "gamesPlayed": int(games_played),
                "botUptime": None,
            })
    except Exception as e:
        return web.json_response({
            "totalUsers": 0, "activeToday": 0, "totalGroups": 0,
            "totalMessages": 0, "bannedUsers": 0, "economyTotal": 0,
            "gamesPlayed": 0, "botUptime": None,
        })


@require_admin
async def users_handler(request):
    try:
        page = int(request.rel_url.query.get("page", 1))
        limit = min(int(request.rel_url.query.get("limit", 20)), 100)
        search = request.rel_url.query.get("search", "")
        banned_filter = request.rel_url.query.get("banned", "")
        offset = (page - 1) * limit

        async with async_session_maker() as session:
            query = select(User)
            count_query = select(func.count()).select_from(User)

            if search:
                like = f"%{search}%"
                cond = or_(
                    User.username.ilike(like),
                    User.first_name.ilike(like),
                    cast(User.id, String).contains(search),
                )
                query = query.where(cond)
                count_query = count_query.where(cond)

            if banned_filter == "true":
                query = query.where(User.is_banned == True)
                count_query = count_query.where(User.is_banned == True)
            elif banned_filter == "false":
                query = query.where(User.is_banned == False)
                count_query = count_query.where(User.is_banned == False)

            total = await session.scalar(count_query)
            result = await session.execute(
                query.order_by(desc(User.last_seen)).offset(offset).limit(limit)
            )
            users = result.scalars().all()

            user_ids = [u.id for u in users]
            wallets = {}
            economies = {}
            if user_ids:
                w_res = await session.execute(select(Wallet).where(Wallet.user_id.in_(user_ids)))
                for w in w_res.scalars():
                    wallets[w.user_id] = w
                e_res = await session.execute(select(Economy).where(Economy.user_id.in_(user_ids)))
                for e in e_res.scalars():
                    economies[e.user_id] = e

            return web.json_response({
                "users": [_user_to_dict(u, wallets.get(u.id), economies.get(u.id)) for u in users],
                "total": total or 0,
                "page": page,
                "limit": limit,
            })
    except Exception as e:
        return web.json_response({"users": [], "total": 0, "page": 1, "limit": 20})


@require_admin
async def ban_user_handler(request):
    try:
        user_id = int(request.match_info["user_id"])
        body = await request.json()
        reason = body.get("reason", "Admin action")
        async with async_session_maker() as session:
            user = await session.get(User, user_id)
            if not user:
                return web.json_response({"success": False, "message": "User not found"}, status=404)
            user.is_banned = True
            await session.commit()
        return web.json_response({"success": True, "message": f"User {user_id} banned"})
    except Exception as e:
        return web.json_response({"success": False, "message": str(e)}, status=500)


@require_admin
async def unban_user_handler(request):
    try:
        user_id = int(request.match_info["user_id"])
        async with async_session_maker() as session:
            user = await session.get(User, user_id)
            if not user:
                return web.json_response({"success": False, "message": "User not found"}, status=404)
            user.is_banned = False
            await session.commit()
        return web.json_response({"success": True, "message": f"User {user_id} unbanned"})
    except Exception as e:
        return web.json_response({"success": False, "message": str(e)}, status=500)


@require_admin
async def groups_handler(request):
    try:
        page = int(request.rel_url.query.get("page", 1))
        limit = min(int(request.rel_url.query.get("limit", 20)), 100)
        search = request.rel_url.query.get("search", "")
        offset = (page - 1) * limit

        async with async_session_maker() as session:
            query = select(Group)
            count_query = select(func.count()).select_from(Group)
            if search:
                like = f"%{search}%"
                cond = or_(Group.title.ilike(like), Group.username.ilike(like))
                query = query.where(cond)
                count_query = count_query.where(cond)

            total = await session.scalar(count_query)
            result = await session.execute(
                query.order_by(desc(Group.created_at)).offset(offset).limit(limit)
            )
            groups = result.scalars().all()
            return web.json_response({
                "groups": [_group_to_dict(g) for g in groups],
                "total": total or 0,
                "page": page,
                "limit": limit,
            })
    except Exception as e:
        import structlog
        structlog.get_logger().exception("Owner groups API failed", error=str(e))
        return web.json_response({"error": "Could not load groups from the database."}, status=500)


@require_admin
async def economy_leaderboard_handler(request):
    try:
        limit = min(int(request.rel_url.query.get("limit", 20)), 100)
        async with async_session_maker() as session:
            result = await session.execute(
                select(User, Wallet, Economy)
                .join(Wallet, Wallet.user_id == User.id)
                .outerjoin(Economy, Economy.user_id == User.id)
                .order_by(desc(Wallet.balance))
                .limit(limit)
            )
            rows = result.all()
            data = []
            for user, wallet, economy in rows:
                data.append({
                    "userId": str(user.id),
                    "username": user.username,
                    "firstName": user.first_name,
                    "balance": wallet.balance if wallet else 0,
                    "level": economy.level if economy else 1,
                    "xp": economy.total_xp if economy else 0,
                })
            return web.json_response(data)
    except Exception as e:
        return web.json_response([])


@require_admin
async def logs_handler(request):
    try:
        page = int(request.rel_url.query.get("page", 1))
        limit = min(int(request.rel_url.query.get("limit", 50)), 200)
        group_id = request.rel_url.query.get("group_id", "")
        action = request.rel_url.query.get("action", "")
        offset = (page - 1) * limit

        async with async_session_maker() as session:
            query = select(ActionLog)
            count_query = select(func.count()).select_from(ActionLog)
            if group_id:
                query = query.where(ActionLog.group_id == int(group_id))
                count_query = count_query.where(ActionLog.group_id == int(group_id))
            if action:
                query = query.where(ActionLog.action == action)
                count_query = count_query.where(ActionLog.action == action)

            total = await session.scalar(count_query)
            result = await session.execute(
                query.order_by(desc(ActionLog.created_at)).offset(offset).limit(limit)
            )
            logs = result.scalars().all()
            log_list = []
            for log in logs:
                details = log.details or {}
                log_list.append({
                    "id": log.id,
                    "action": log.action,
                    "performedBy": str(log.admin_id) if log.admin_id else "system",
                    "targetId": str(log.user_id) if log.user_id else "",
                    "groupId": str(log.group_id) if log.group_id else "",
                    "reason": details.get("reason") if isinstance(details, dict) else None,
                    "details": str(details) if details else None,
                    "createdAt": log.created_at.isoformat() if log.created_at else None,
                })
            return web.json_response({
                "logs": log_list,
                "total": total or 0,
                "page": page,
                "limit": limit,
            })
    except Exception as e:
        return web.json_response({"logs": [], "total": 0, "page": 1, "limit": 50})


@require_admin
async def broadcast_handler(request):
    try:
        body = await request.json()
        message_text = body.get("message", "")
        parse_mode = body.get("parseMode", "HTML")
        if not message_text:
            return web.json_response({"sent": 0, "failed": 0, "total": 0})

        from bot.config import settings
        from aiogram import Bot
        from aiogram.client.default import DefaultBotProperties
        from aiogram.enums import ParseMode as AiogramParseMode

        bot = Bot(
            token=settings.BOT_TOKEN,
            default=DefaultBotProperties(parse_mode=AiogramParseMode.HTML),
        )
        async with async_session_maker() as session:
            result = await session.execute(
                select(Group.id).where(Group.is_active == True)
            )
            group_ids = result.scalars().all()

        sent = 0
        failed = 0
        for gid in group_ids:
            try:
                await bot.send_message(gid, message_text)
                sent += 1
            except Exception:
                failed += 1
        await bot.session.close()
        return web.json_response({"sent": sent, "failed": failed, "total": len(group_ids)})
    except Exception as e:
        return web.json_response({"sent": 0, "failed": 0, "total": 0}, status=500)


@require_admin
async def owner_dm_broadcast_handler(request):
    """Send a plain-text DM only to users who explicitly started the bot."""
    bot = None
    try:
        body = await request.json()
        message_text = str(body.get("message", "")).strip()
        if not message_text or len(message_text) > 4000:
            return web.json_response({"error": "Message must be 1–4000 characters."}, status=400)
        from bot.config import settings
        from aiogram import Bot
        bot = Bot(token=settings.BOT_TOKEN)
        async with async_session_maker() as session:
            result = await session.execute(
                select(User.id).where(User.dm_started.is_(True), User.is_banned.is_(False)).order_by(User.id.asc())
            )
            user_ids = result.scalars().all()
        sent = failed = 0
        for user_id in user_ids:
            try:
                await bot.send_message(chat_id=user_id, text=message_text)
                sent += 1
            except Exception:
                failed += 1
            # Respect Telegram rate limits and avoid flooding the API.
            import asyncio
            await asyncio.sleep(0.05)
        return web.json_response({"sent": sent, "failed": failed, "total": len(user_ids)})
    except Exception:
        return web.json_response({"error": "DM broadcast failed. Check bot connectivity and logs."}, status=500)
    finally:
        if bot is not None:
            await bot.session.close()


@require_admin
async def twa_profile_handler(request):
    try:
        user_id = int(request.rel_url.query.get("userId", 0))
        async with async_session_maker() as session:
            from sqlalchemy import select, func, desc
            user = await session.get(User, user_id)
            if not user:
                return web.json_response({"error": "User not found"}, status=404)
            wallet_res = await session.execute(select(Wallet).where(Wallet.user_id == user_id))
            wallet = wallet_res.scalar_one_or_none()
            econ_res = await session.execute(select(Economy).where(Economy.user_id == user_id))
            economy = econ_res.scalar_one_or_none()
            # Get rank
            rank_res = await session.scalar(
                select(func.count()).select_from(Wallet).where(
                    Wallet.balance > (wallet.balance if wallet else 0)
                )
            )
            rank = (rank_res or 0) + 1
            # Daily/weekly availability
            now = datetime.utcnow()
            daily_avail = True
            weekly_avail = True
            if wallet:
                if wallet.last_daily and (now - wallet.last_daily.replace(tzinfo=None)).total_seconds() < 86400:
                    daily_avail = False
                if wallet.last_weekly and (now - wallet.last_weekly.replace(tzinfo=None)).total_seconds() < 7 * 86400:
                    weekly_avail = False
            return web.json_response({
                "userId": str(user.id),
                "username": user.username,
                "firstName": user.first_name,
                "lastName": user.last_name,
                "balance": wallet.balance if wallet else 0,
                "bankBalance": wallet.bank_balance if wallet else 0,
                "level": economy.level if economy else 1,
                "xp": economy.xp if economy else 0,
                "totalXp": economy.total_xp if economy else 0,
                "rank": rank,
                "isPremium": user.is_premium,
                "isBanned": user.is_banned,
                "language": user.language,
                "dailyAvailable": daily_avail,
                "weeklyAvailable": weekly_avail,
                "streakDays": economy.streak_days if economy else 0,
                "referralCount": economy.referral_count if economy else 0,
                "createdAt": user.created_at.isoformat() if user.created_at else None,
            })
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


@require_admin
async def twa_leaderboard_handler(request):
    try:
        limit = min(int(request.rel_url.query.get("limit", 20)), 50)
        async with async_session_maker() as session:
            from sqlalchemy import select, desc
            result = await session.execute(
                select(User, Wallet, Economy)
                .join(Wallet, Wallet.user_id == User.id)
                .outerjoin(Economy, Economy.user_id == User.id)
                .order_by(desc(Wallet.balance))
                .limit(limit)
            )
            rows = result.all()
            data = []
            for i, (user, wallet, economy) in enumerate(rows):
                data.append({
                    "rank": i + 1,
                    "userId": str(user.id),
                    "username": user.username,
                    "firstName": user.first_name,
                    "balance": wallet.balance if wallet else 0,
                    "level": economy.level if economy else 1,
                    "xp": economy.total_xp if economy else 0,
                    "isPremium": user.is_premium,
                })
            return web.json_response(data)
    except Exception as e:
        return web.json_response([])


@require_admin
async def twa_claim_handler(request):
    try:
        body = await request.json()
        user_id = int(body.get("userId", 0))
        claim_type = body.get("type", "daily")
        async with async_session_maker() as session:
            wallet_res = await session.execute(select(Wallet).where(Wallet.user_id == user_id))
            wallet = wallet_res.scalar_one_or_none()
            if not wallet:
                return web.json_response({"success": False, "message": "User not found in bot", "amount": 0, "nextAvailableAt": None})
            now = datetime.utcnow()
            from bot.config import settings
            if claim_type == "weekly":
                if wallet.last_weekly and (now - wallet.last_weekly.replace(tzinfo=None)).total_seconds() < 7 * 86400:
                    next_at = (wallet.last_weekly.replace(tzinfo=None) + __import__('datetime').timedelta(days=7)).isoformat()
                    return web.json_response({"success": False, "message": "Weekly reward not available yet", "amount": 0, "nextAvailableAt": next_at})
                amount = settings.WEEKLY_REWARD
                wallet.balance += amount
                wallet.last_weekly = now
            else:
                if wallet.last_daily and (now - wallet.last_daily.replace(tzinfo=None)).total_seconds() < 86400:
                    next_at = (wallet.last_daily.replace(tzinfo=None) + __import__('datetime').timedelta(days=1)).isoformat()
                    return web.json_response({"success": False, "message": "Daily reward not available yet", "amount": 0, "nextAvailableAt": next_at})
                amount = settings.DAILY_REWARD
                wallet.balance += amount
                wallet.last_daily = now
            await session.commit()
            next_at = (now + __import__('datetime').timedelta(days=7 if claim_type == "weekly" else 1)).isoformat()
            return web.json_response({"success": True, "message": f"{'Weekly' if claim_type == 'weekly' else 'Daily'} reward claimed! +{amount} coins", "amount": amount, "nextAvailableAt": next_at})
    except Exception as e:
        return web.json_response({"success": False, "message": str(e), "amount": 0, "nextAvailableAt": None}, status=500)


@require_admin
async def twa_game_stats_handler(request):
    try:
        user_id = int(request.rel_url.query.get("userId", 0))
        async with async_session_maker() as session:
            from bot.database.models import DuelStats, GameStats as GameStatsModel
            try:
                duel_res = await session.execute(select(DuelStats).where(DuelStats.user_id == user_id))
                duel = duel_res.scalar_one_or_none()
                game_res = await session.execute(select(GameStatsModel).where(GameStatsModel.user_id == user_id))
                game = game_res.scalar_one_or_none()
                won = duel.wins if duel else 0
                lost = duel.losses if duel else 0
                total = won + lost
                return web.json_response({
                    "userId": str(user_id),
                    "duelsWon": won,
                    "duelsLost": lost,
                    "gamesPlayed": game.total_games if game else 0,
                    "totalWinnings": duel.total_won if duel else 0,
                    "winRate": round(won / total * 100, 1) if total > 0 else 0,
                })
            except Exception:
                return web.json_response({"userId": str(user_id), "duelsWon": 0, "duelsLost": 0, "gamesPlayed": 0, "totalWinnings": 0, "winRate": 0})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


def _verify_owner_telegram_init_data(init_data: str) -> bool:
    """Verify Telegram Mini App initData and require the configured bot owner."""
    from bot.config import settings

    token = (settings.BOT_TOKEN or "").strip()
    owner_id = int(settings.effective_owner_id or 0)
    if not token or not owner_id or not init_data or len(init_data) > 8192:
        return False
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
        supplied_hash = pairs.pop("hash", "")
        auth_date = int(pairs.get("auth_date", "0"))
        if not supplied_hash or auth_date <= 0 or time.time() - auth_date > 86400 or auth_date > time.time() + 60:
            return False
        data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs.items()))
        secret_key = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        expected_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected_hash, supplied_hash):
            return False
        telegram_user = json.loads(pairs.get("user", "{}"))
        return int(telegram_user.get("id", 0)) == owner_id
    except (ValueError, TypeError, json.JSONDecodeError):
        return False


async def owner_app_handler(request):
    """Serve the owner UI shell; all private data remains behind server verification."""
    from bot.config import settings
    if not settings.effective_owner_id or not settings.BOT_TOKEN:
        return web.Response(text="Owner Mini App is not configured.", status=503)
    app_file = Path(__file__).resolve().parent.parent / "web" / "owner" / "index.html"
    if not app_file.is_file():
        return web.Response(text="Owner Mini App files are missing.", status=404)
    return web.FileResponse(app_file, headers={"Cache-Control": "no-store"})


async def owner_overview_handler(request):
    init_data = request.headers.get("X-Telegram-Init-Data", "")
    if not _verify_owner_telegram_init_data(init_data):
        return web.json_response({"error": "Unauthorized"}, status=401, headers={"Cache-Control": "no-store"})
    try:
        # Resolve the lazy database session maker explicitly in this function.
        # Module-level __getattr__ does not resolve unbound global names.
        from bot.database.connection import async_session_maker

        # User.last_seen is timezone-aware in the model; compare using aware UTC.
        today = datetime.now(timezone.utc) - timedelta(hours=24)
        async with async_session_maker() as session:
            total_users = await session.scalar(select(func.count()).select_from(User))
            active_today = await session.scalar(
                select(func.count()).select_from(User).where(User.last_seen >= today)
            )
            total_groups = await session.scalar(select(func.count()).select_from(Group))
            banned_users = await session.scalar(
                select(func.count()).select_from(User).where(User.is_banned.is_(True))
            )
        return web.json_response({
            "totalUsers": int(total_users or 0),
            "activeToday": int(active_today or 0),
            "totalGroups": int(total_groups or 0),
            "bannedUsers": int(banned_users or 0),
        }, headers={"Cache-Control": "no-store"})
    except Exception:
        import logging
        logging.getLogger(__name__).exception("Owner Mini App dashboard query failed")
        return web.json_response({"error": "Dashboard data is temporarily unavailable."}, status=503)




@require_admin
async def owner_security_status(request):
    async with async_session_maker() as session:
        groups = (await session.scalars(select(Group).where(Group.is_active.is_(True)))).all()
        keys = ("anti_spam", "anti_flood", "emergency_mode")
        counts = {key: 0 for key in keys}
        for group in groups:
            row = await session.scalar(select(SecuritySettings).where(SecuritySettings.group_id == group.id))
            if row:
                for key in keys:
                    counts[key] += int(bool(getattr(row, key, False)))
        return web.json_response({"totalGroups": len(groups), "counts": counts})

@require_admin
async def owner_security_update(request):
    body = await request.json()
    key, enabled = body.get("field"), body.get("enabled")
    if key not in {"anti_spam", "anti_flood", "emergency_mode"} or not isinstance(enabled, bool):
        return web.json_response({"error": "Invalid setting"}, status=400)
    async with async_session_maker() as session:
        groups = (await session.scalars(select(Group).where(Group.is_active.is_(True)))).all()
        for group in groups:
            row = await session.scalar(select(SecuritySettings).where(SecuritySettings.group_id == group.id))
            if row is None:
                row = SecuritySettings(group_id=group.id)
                session.add(row)
            setattr(row, key, enabled)
        await session.commit()
    return web.json_response({"success": True, "updatedGroups": len(groups), "field": key, "enabled": enabled})



@require_admin
async def owner_referrals_handler(request):
    async with async_session_maker() as session:
        total = await session.scalar(select(func.count()).select_from(Referral)) or 0
        rewarded = await session.scalar(select(func.count()).select_from(Referral).where(Referral.reward_given.is_(True))) or 0
        pending = await session.scalar(select(func.count()).select_from(AffiliateRequest).where(AffiliateRequest.status == "pending")) or 0
        rows = (await session.execute(
            select(Referral, User).join(User, User.id == Referral.referrer_id)
            .order_by(desc(Referral.created_at)).limit(100)
        )).all()
        referrals = [{
            "id": r.id, "referrerId": str(r.referrer_id), "referredId": str(r.referred_id),
            "referrer": u.first_name or u.username or str(u.id),
            "rewardGiven": bool(r.reward_given),
            "createdAt": r.created_at.isoformat() if r.created_at else None
        } for r, u in rows]
        requests = (await session.execute(
            select(AffiliateRequest, User).join(User, User.id == AffiliateRequest.user_id)
            .where(AffiliateRequest.status == "pending").order_by(desc(AffiliateRequest.created_at)).limit(100)
        )).all()
        payouts = [{
            "id": x.id, "userId": str(x.user_id), "name": u.first_name or str(u.id),
            "type": x.request_type, "stars": x.requested_stars, "status": x.status,
            "createdAt": x.created_at.isoformat() if x.created_at else None
        } for x, u in requests]
    return web.json_response({"total": int(total), "rewarded": int(rewarded), "pendingRequests": int(pending), "referrals": referrals, "requests": payouts}, headers={"Cache-Control": "no-store"})


@require_admin
async def owner_affiliate_request_update(request):
    try:
        request_id = int(request.match_info["request_id"])
        body = await request.json()
        status = body.get("status")
        if status not in {"approved", "rejected"}:
            return web.json_response({"error": "Status must be approved or rejected."}, status=400)
        async with async_session_maker() as session:
            item = await session.get(AffiliateRequest, request_id)
            if not item or item.status != "pending":
                return web.json_response({"error": "Pending request not found."}, status=404)
            item.status = status
            item.admin_reply = "Reviewed in VIP Owner Mini App"
            await session.commit()
        return web.json_response({"success": True, "id": request_id, "status": status})
    except (ValueError, TypeError):
        return web.json_response({"error": "Invalid request."}, status=400)


@require_admin
async def owner_system_controls_handler(request):
    """Read and update the existing persistent owner AI and maintenance switches."""
    async with async_session_maker() as session:
        ai_row = await session.get(GlobalSetting, "owner_ai_enabled")
        maintenance_row = await session.get(GlobalSetting, "owner_maintenance_mode")
        return web.json_response({
            "aiEnabled": bool(ai_row and ai_row.value == "1"),
            "maintenanceEnabled": bool(maintenance_row and maintenance_row.value == "1"),
        }, headers={"Cache-Control": "no-store"})


@require_admin
async def owner_system_controls_update_handler(request):
    try:
        body = await request.json()
        field = body.get("field")
        enabled = body.get("enabled")
        if field not in {"ai", "maintenance"} or not isinstance(enabled, bool):
            return web.json_response({"error": "Invalid system control."}, status=400)
        key = "owner_ai_enabled" if field == "ai" else "owner_maintenance_mode"
        async with async_session_maker() as session:
            row = await session.get(GlobalSetting, key)
            if row is None:
                session.add(GlobalSetting(key=key, value="1" if enabled else "0"))
            else:
                row.value = "1" if enabled else "0"
            await session.commit()
        if field == "maintenance":
            # Keep the running bot's existing in-memory gate in sync with its DB setting.
            from bot.handlers import owner as owner_handler
            owner_handler._maintenance_mode = enabled
        return web.json_response({"success": True, "field": field, "enabled": enabled})
    except Exception:
        return web.json_response({"error": "Could not update system control."}, status=500)


@require_admin
async def owner_promotion_requests_handler(request):
    async with async_session_maker() as session:
        rows = (await session.scalars(
            select(GroupPromotionRequest).order_by(desc(GroupPromotionRequest.requested_at)).limit(100)
        )).all()
    return web.json_response({"requests": [{
        "id": x.id, "userId": str(x.user_id), "groupId": str(x.group_id),
        "title": x.group_title, "link": x.group_link, "status": x.status,
        "requestedAt": x.requested_at.isoformat() if x.requested_at else None
    } for x in rows]}, headers={"Cache-Control": "no-store"})


@require_admin
async def owner_promotion_request_update(request):
    try:
        request_id = int(request.match_info["request_id"])
        body = await request.json()
        status = body.get("status")
        if status not in {"approved", "rejected"}:
            return web.json_response({"error": "Status must be approved or rejected."}, status=400)
        async with async_session_maker() as session:
            item = await session.get(GroupPromotionRequest, request_id)
            if not item or item.status != "pending":
                return web.json_response({"error": "Pending request not found."}, status=404)
            item.status = status
            if status == "approved":
                item.approved_at = datetime.utcnow()
            await session.commit()
        return web.json_response({"success": True, "id": request_id, "status": status})
    except (ValueError, TypeError):
        return web.json_response({"error": "Invalid request."}, status=400)



def _decode_owner_media(preview):
    if not preview or not preview.startswith("__ALEXA_MEDIA__"):
        return None
    try:
        value = json.loads(preview[len("__ALEXA_MEDIA__"):])
        if not isinstance(value, dict) or not value.get("file_id"):
            return None
        return value
    except (TypeError, ValueError):
        return None


@require_admin
async def owner_inbox_media_handler(request):
    return await _owner_media_response(request, "private")


@require_admin
async def owner_group_media_handler(request):
    return await _owner_media_response(request, "group")


async def _owner_media_response(request, scope):
    bot = None
    try:
        record_id = int(request.match_info["record_id"])
        if scope == "private":
            user_id = int(request.match_info["user_id"])
            async with async_session_maker() as session:
                item = await session.scalar(select(InboxMessage).where(
                    InboxMessage.id == record_id, InboxMessage.user_id == user_id
                ))
                if not item:
                    return web.json_response({"error": "Inbox media not found."}, status=404)
                media = _decode_owner_media(item.preview)
        else:
            group_id = int(request.match_info["group_id"])
            async with async_session_maker() as session:
                group = await session.get(Group, group_id)
                item = await session.scalar(select(GroupInboxMessage).where(
                    GroupInboxMessage.id == record_id, GroupInboxMessage.chat_id == group_id
                ))
                if not group or not group.is_active or not item:
                    return web.json_response({"error": "Group media not found."}, status=404)
                media = _decode_owner_media(item.preview)
        if not media:
            return web.json_response({"error": "This message has no stored media preview."}, status=404)
        from io import BytesIO
        from bot.config import settings
        from aiogram import Bot
        bot = Bot(token=settings.BOT_TOKEN)
        telegram_file = await bot.get_file(media["file_id"])
        if not telegram_file.file_path:
            return web.json_response({"error": "Telegram media is unavailable."}, status=404)
        buffer = BytesIO()
        await bot.download_file(telegram_file.file_path, destination=buffer)
        kind = media.get("kind")
        fallback_types = {"photo": "image/jpeg", "sticker": "image/webp", "video_note": "video/mp4"}
        content_type = media.get("mime_type") or fallback_types.get(kind) or "application/octet-stream"
        return web.Response(
            body=buffer.getvalue(), content_type=content_type,
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
        )
    except (ValueError, TypeError):
        return web.json_response({"error": "Invalid media reference."}, status=400)
    except Exception:
        return web.json_response({"error": "Telegram could not retrieve this media."}, status=502)
    finally:
        if bot is not None:
            await bot.session.close()


@require_admin
async def owner_avatar_handler(request):
    bot = None
    try:
        user_id = int(request.match_info["user_id"])
        from io import BytesIO
        from bot.config import settings
        from aiogram import Bot
        bot = Bot(token=settings.BOT_TOKEN)
        photos = await bot.get_user_profile_photos(user_id=user_id, limit=1)
        if not photos.photos or not photos.photos[0]:
            return web.json_response({"error": "No Telegram profile photo is available."}, status=404)
        photo = photos.photos[0][-1]
        telegram_file = await bot.get_file(photo.file_id)
        if not telegram_file.file_path:
            return web.json_response({"error": "Telegram profile photo is unavailable."}, status=404)
        buffer = BytesIO()
        await bot.download_file(telegram_file.file_path, destination=buffer)
        return web.Response(
            body=buffer.getvalue(), content_type="image/jpeg",
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
        )
    except (ValueError, TypeError):
        return web.json_response({"error": "Invalid Telegram user ID."}, status=400)
    except Exception:
        return web.json_response({"error": "Telegram profile photo could not be retrieved."}, status=502)
    finally:
        if bot is not None:
            await bot.session.close()


@require_admin
async def owner_inbox_handler(request):
    """List every user known to have opened a private chat with ALEXA."""
    try:
        page = max(1, int(request.rel_url.query.get("page", "1")))
        limit = min(500, max(1, int(request.rel_url.query.get("limit", "200"))))
    except (ValueError, TypeError):
        return web.json_response({"error": "Invalid inbox pagination."}, status=400)

    async with async_session_maker() as session:
        # Older installations may have inbox history from before dm_started
        # was tracked. Treat any stored private message as proof of a started DM.
        started_filter = or_(
            User.dm_started.is_(True),
            User.id.in_(select(InboxMessage.user_id).distinct()),
        )
        total = await session.scalar(
            select(func.count()).select_from(User).where(started_filter)
        ) or 0
        users = (await session.scalars(
            select(User)
            .where(started_filter)
            .order_by(desc(User.last_seen), desc(User.id))
            .offset((page - 1) * limit)
            .limit(limit)
        )).all()

        data = []
        for user in users:
            item = await session.scalar(
                select(InboxMessage)
                .where(InboxMessage.user_id == user.id)
                .order_by(desc(InboxMessage.created_at), desc(InboxMessage.id))
                .limit(1)
            )
            unread = await session.scalar(
                select(func.count()).select_from(InboxMessage).where(
                    InboxMessage.user_id == user.id,
                    InboxMessage.is_read.is_(False),
                )
            ) or 0
            media = _decode_owner_media(item.preview) if item else None
            preview = ((media or {}).get("caption") or "[" + item.content_type + "]") if media else (item.preview if item else "No messages yet — start a conversation")
            data.append({
                "userId": str(user.id),
                "name": user.first_name or "User",
                "username": user.username,
                "unread": int(unread),
                "preview": preview,
                "avatarUrl": f"/owner-api/avatar/{user.id}",
                "createdAt": item.created_at.isoformat() if item and item.created_at else (user.last_seen.isoformat() if user.last_seen else None),
            })
    return web.json_response({
        "conversations": data, "total": int(total), "page": page, "limit": limit
    }, headers={"Cache-Control": "no-store"})


@require_admin
async def owner_inbox_messages_handler(request):
    try:
        user_id = int(request.match_info["user_id"])
        before_id = int(request.query.get("before", "0") or 0)
        limit = min(100, max(1, int(request.query.get("limit", "50"))))
    except (ValueError, TypeError):
        return web.json_response({"error": "Invalid conversation request."}, status=400)
    async with async_session_maker() as session:
        user = await session.get(User, user_id)
        if not user or not user.dm_started:
            return web.json_response({"error": "Conversation not found."}, status=404)
        query = select(InboxMessage).where(InboxMessage.user_id == user_id)
        if before_id:
            query = query.where(InboxMessage.id < before_id)
        rows = (await session.scalars(query.order_by(desc(InboxMessage.id)).limit(limit))).all()
        for row in rows:
            row.is_read = True
        await session.commit()
        messages = [{
            "id": item.id, "messageId": str(item.telegram_message_id),
            "text": (item.preview or "[" + item.content_type + "]") if not _decode_owner_media(item.preview) else (_decode_owner_media(item.preview).get("caption") or "[" + item.content_type + "]"),
            "media": bool(_decode_owner_media(item.preview)),
            "mediaUrl": f"/owner-api/inbox/{user_id}/messages/{item.id}/media" if _decode_owner_media(item.preview) else None,
            "contentType": item.content_type,
            "createdAt": item.created_at.isoformat() if item.created_at else None,
            "direction": "outgoing" if item.content_type == "outgoing_text" else "incoming"
        } for item in reversed(rows)]
    return web.json_response({"messages": messages, "hasMore": len(rows) == limit},
                             headers={"Cache-Control": "no-store"})


@require_admin
async def owner_inbox_delete_message_handler(request):
    bot = None
    try:
        user_id = int(request.match_info["user_id"])
        record_id = int(request.match_info["message_id"])
    except (ValueError, TypeError):
        return web.json_response({"error": "Invalid message reference."}, status=400)
    async with async_session_maker() as session:
        item = await session.scalar(select(InboxMessage).where(
            InboxMessage.id == record_id, InboxMessage.user_id == user_id
        ))
        if not item:
            return web.json_response({"error": "Message not found in inbox history."}, status=404)
        telegram_message_id = item.telegram_message_id
    try:
        from bot.config import settings
        from aiogram import Bot
        bot = Bot(token=settings.BOT_TOKEN)
        await bot.delete_message(chat_id=user_id, message_id=telegram_message_id)
        async with async_session_maker() as session:
            item = await session.scalar(select(InboxMessage).where(
                InboxMessage.id == record_id, InboxMessage.user_id == user_id
            ))
            if item:
                await session.delete(item)
                await session.commit()
        return web.json_response({"success": True})
    except Exception:
        return web.json_response({"error": "Telegram could not delete this message. It may be too old or unavailable."}, status=502)
    finally:
        if bot is not None:
            await bot.session.close()


@require_admin
async def owner_group_messages_handler(request):
    try:
        group_id = int(request.match_info["group_id"])
        before_id = int(request.query.get("before", "0") or 0)
        limit = min(100, max(1, int(request.query.get("limit", "50"))))
    except (ValueError, TypeError):
        return web.json_response({"error": "Invalid group history request."}, status=400)
    async with async_session_maker() as session:
        group = await session.get(Group, group_id)
        if not group or not group.is_active:
            return web.json_response({"error": "Active group not found."}, status=404)
        query = select(GroupInboxMessage).where(GroupInboxMessage.chat_id == group_id)
        sender_id_raw = request.query.get("sender_id")
        if sender_id_raw:
            try:
                query = query.where(GroupInboxMessage.sender_id == int(sender_id_raw))
            except (ValueError, TypeError):
                return web.json_response({"error": "Invalid sender ID."}, status=400)
        if before_id:
            query = query.where(GroupInboxMessage.id < before_id)
        rows = (await session.scalars(query.order_by(desc(GroupInboxMessage.id)).limit(limit))).all()
        messages = [{
            "id": item.id, "messageId": str(item.telegram_message_id),
            "senderId": str(item.sender_id) if item.sender_id else None,
            "senderName": item.sender_name or "Unknown",
            "username": item.sender_username,
            "text": (item.preview or "[" + item.content_type + "]") if not _decode_owner_media(item.preview) else (_decode_owner_media(item.preview).get("caption") or "[" + item.content_type + "]"),
            "media": bool(_decode_owner_media(item.preview)),
            "mediaUrl": f"/owner-api/groups/{group_id}/messages/{item.id}/media" if _decode_owner_media(item.preview) else None,
            "contentType": item.content_type,
            "createdAt": item.created_at.isoformat() if item.created_at else None
        } for item in reversed(rows)]
    return web.json_response({"group": _group_to_dict(group), "messages": messages,
                              "hasMore": len(rows) == limit},
                             headers={"Cache-Control": "no-store"})


@require_admin
async def owner_group_delete_message_handler(request):
    bot = None
    try:
        group_id = int(request.match_info["group_id"])
        record_id = int(request.match_info["message_id"])
    except (ValueError, TypeError):
        return web.json_response({"error": "Invalid message reference."}, status=400)
    async with async_session_maker() as session:
        group = await session.get(Group, group_id)
        item = await session.scalar(select(GroupInboxMessage).where(
            GroupInboxMessage.id == record_id, GroupInboxMessage.chat_id == group_id
        ))
        if not group or not group.is_active or not item:
            return web.json_response({"error": "Active group message not found."}, status=404)
        telegram_message_id = item.telegram_message_id
    try:
        from bot.config import settings
        from aiogram import Bot
        bot = Bot(token=settings.BOT_TOKEN)
        me = await bot.get_me()
        member = await bot.get_chat_member(group_id, me.id)
        status = str(getattr(member.status, "value", member.status))
        if status not in ("administrator", "creator") or (status != "creator" and not getattr(member, "can_delete_messages", False)):
            return web.json_response({"error": "ALEXA does not currently have Delete Messages permission."}, status=403)
        await bot.delete_message(chat_id=group_id, message_id=telegram_message_id)
        async with async_session_maker() as session:
            item = await session.scalar(select(GroupInboxMessage).where(
                GroupInboxMessage.id == record_id, GroupInboxMessage.chat_id == group_id
            ))
            if item:
                await session.delete(item)
                await session.commit()
        return web.json_response({"success": True})
    except Exception:
        return web.json_response({"error": "Telegram could not delete this group message."}, status=502)
    finally:
        if bot is not None:
            await bot.session.close()


@require_admin
async def owner_statistics_handler(request):
    async with async_session_maker() as session:
        values = {
            "users": await session.scalar(select(func.count()).select_from(User)) or 0,
            "dmStarters": await session.scalar(select(func.count()).select_from(User).where(User.dm_started.is_(True))) or 0,
            "groups": await session.scalar(select(func.count()).select_from(Group).where(Group.is_active.is_(True))) or 0,
            "memberships": await session.scalar(select(func.count()).select_from(GroupMember)) or 0,
            "referrals": await session.scalar(select(func.count()).select_from(Referral)) or 0,
            "pendingPromotions": await session.scalar(select(func.count()).select_from(GroupPromotionRequest).where(GroupPromotionRequest.status == "pending")) or 0
        }
    return web.json_response(values, headers={"Cache-Control": "no-store"})



@require_admin
async def owner_health_handler(request):
    try:
        async with async_session_maker() as session:
            await session.execute(select(1))
            active_groups = await session.scalar(select(func.count()).select_from(Group).where(Group.is_active.is_(True))) or 0
            dm_users = await session.scalar(select(func.count()).select_from(User).where(User.dm_started.is_(True))) or 0
            pending_reviews = await session.scalar(select(func.count()).select_from(GroupPromotionRequest).where(GroupPromotionRequest.status == "pending")) or 0
        return web.json_response({"process": "running", "database": "connected", "activeGroups": int(active_groups), "dmStarters": int(dm_users), "pendingPromotionRequests": int(pending_reviews)}, headers={"Cache-Control": "no-store"})
    except Exception:
        return web.json_response({"process": "running", "database": "check_failed"}, status=503, headers={"Cache-Control": "no-store"})


@require_admin
async def owner_group_message_handler(request):
    bot = None
    try:
        body = await request.json()
        message_text = str(body.get("message", "")).strip()
        raw_ids = body.get("groupIds", [])
        if not message_text or len(message_text) > 4000:
            return web.json_response({"error": "Message must be 1–4000 characters."}, status=400)
        if not isinstance(raw_ids, list) or not raw_ids or len(raw_ids) > 100:
            return web.json_response({"error": "Select between 1 and 100 groups."}, status=400)
        try:
            group_ids = list(dict.fromkeys(int(value) for value in raw_ids))
        except (ValueError, TypeError):
            return web.json_response({"error": "Invalid group selection."}, status=400)
        async with async_session_maker() as session:
            groups = (await session.scalars(select(Group).where(Group.id.in_(group_ids), Group.is_active.is_(True)))).all()
        if len(groups) != len(group_ids):
            return web.json_response({"error": "One or more selected groups are not active registered groups."}, status=400)
        from bot.config import settings
        from aiogram import Bot
        import asyncio
        bot = Bot(token=settings.BOT_TOKEN)
        sent = failed = 0
        for group in groups:
            try:
                await bot.send_message(chat_id=group.id, text=message_text)
                sent += 1
            except Exception:
                failed += 1
            await asyncio.sleep(0.05)
        return web.json_response({"sent": sent, "failed": failed, "total": len(groups)})
    except Exception:
        return web.json_response({"error": "Group message failed. Check bot connectivity and logs."}, status=500)
    finally:
        if bot is not None:
            await bot.session.close()



@require_admin
async def owner_inbox_reply_handler(request):
    bot = None
    try:
        user_id = int(request.match_info["user_id"])
        body = await request.json()
        message_text = str(body.get("message", "")).strip()
        if not message_text or len(message_text) > 4000:
            return web.json_response({"error": "Reply must be 1–4000 characters."}, status=400)
        async with async_session_maker() as session:
            user = await session.get(User, user_id)
            if not user or not user.dm_started or user.is_banned:
                return web.json_response({"error": "User is not an eligible DM starter."}, status=404)
        from bot.config import settings
        from aiogram import Bot
        bot = Bot(token=settings.BOT_TOKEN)
        sent_message = await bot.send_message(chat_id=user_id, text=message_text)
        # Keep the owner's sent reply in the same conversation timeline.
        async with async_session_maker() as session:
            session.add(InboxMessage(
                user_id=user_id,
                telegram_message_id=sent_message.message_id,
                content_type="outgoing_text",
                preview=message_text[:1000],
                is_read=True,
            ))
            await session.commit()
        return web.json_response({"success": True, "userId": str(user_id), "messageId": sent_message.message_id})
    except (ValueError, TypeError):
        return web.json_response({"error": "Invalid user ID."}, status=400)
    except Exception:
        return web.json_response({"error": "Telegram could not deliver the reply."}, status=502)
    finally:
        if bot is not None:
            await bot.session.close()



@require_admin
async def owner_group_avatar_handler(request):
    bot = None
    try:
        group_id = int(request.match_info["group_id"])
        from io import BytesIO
        from bot.config import settings
        from aiogram import Bot
        bot = Bot(token=settings.BOT_TOKEN)
        chat = await bot.get_chat(chat_id=group_id)
        photo = getattr(chat, "photo", None)
        file_id = (photo.big_file_id or photo.small_file_id) if photo else None
        if not file_id:
            return web.json_response({"error": "No Telegram group photo is available."}, status=404)
        telegram_file = await bot.get_file(file_id)
        if not telegram_file.file_path:
            return web.json_response({"error": "Telegram group photo is unavailable."}, status=404)
        buffer = BytesIO()
        await bot.download_file(telegram_file.file_path, destination=buffer)
        return web.Response(body=buffer.getvalue(), content_type="image/jpeg",
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})
    except (ValueError, TypeError):
        return web.json_response({"error": "Invalid Telegram group ID."}, status=400)
    except Exception:
        return web.json_response({"error": "Telegram group photo could not be retrieved."}, status=502)
    finally:
        if bot is not None:
            await bot.session.close()


@require_admin
async def owner_group_members_handler(request):
    try:
        group_id = int(request.match_info["group_id"])
        page = max(1, int(request.rel_url.query.get("page", "1")))
        limit = min(100, max(1, int(request.rel_url.query.get("limit", "50"))))
        search = (request.rel_url.query.get("search", "") or "").strip()
    except (ValueError, TypeError):
        return web.json_response({"error": "Invalid group member request."}, status=400)
    async with async_session_maker() as session:
        group = await session.get(Group, group_id)
        if not group or not group.is_active:
            return web.json_response({"error": "Active group not found."}, status=404)
        query = select(GroupMember, User).join(User, User.id == GroupMember.user_id).where(
            GroupMember.group_id == group_id, User.is_bot.is_(False),
            GroupMember.role.notin_(("left", "kicked")))
        count_query = select(func.count()).select_from(GroupMember).join(
            User, User.id == GroupMember.user_id).where(
            GroupMember.group_id == group_id, User.is_bot.is_(False),
            GroupMember.role.notin_(("left", "kicked")))
        if search:
            like = f"%{search}%"
            condition = or_(User.first_name.ilike(like), User.last_name.ilike(like),
                User.username.ilike(like), cast(User.id, String).contains(search))
            query = query.where(condition)
            count_query = count_query.where(condition)
        total = await session.scalar(count_query) or 0
        rows = (await session.execute(query.order_by(
            desc(GroupMember.last_activity), desc(GroupMember.message_count)
        ).offset((page - 1) * limit).limit(limit))).all()
        members = [{
            "id": str(member.user_id),
            "name": " ".join(part for part in (user.first_name, user.last_name) if part).strip() or "Telegram user",
            "username": user.username, "role": member.role or "member",
            "messageCount": int(member.message_count or 0),
            "lastActivity": member.last_activity.isoformat() if member.last_activity else None,
            "isMuted": bool(member.is_muted), "isBanned": bool(member.is_banned),
            "avatarUrl": f"/owner-api/avatar/{member.user_id}",
        } for member, user in rows]
    return web.json_response({
        "group": _group_to_dict(group), "members": members, "total": int(total),
        "page": page, "limit": limit, "source": "tracked",
        "note": "Telegram Bot API does not provide a complete group-member directory. Alexa lists members it has observed through messages, join updates, or moderation activity.",
    }, headers={"Cache-Control": "no-store"})



@require_admin
async def owner_group_activity_handler(request):
    try:
        group_id = int(request.match_info["group_id"])
    except (ValueError, TypeError):
        return web.json_response({"error": "Invalid group ID."}, status=400)
    async with async_session_maker() as session:
        group = await session.get(Group, group_id)
        if not group or not group.is_active:
            return web.json_response({"error": "Active group not found."}, status=404)
        member_count = await session.scalar(select(func.count()).select_from(GroupMember).where(GroupMember.group_id == group_id)) or 0
        logs = (await session.scalars(select(ActionLog).where(ActionLog.group_id == group_id).order_by(desc(ActionLog.created_at)).limit(20))).all()
        security = await session.scalar(select(SecuritySettings).where(SecuritySettings.group_id == group_id))
    return web.json_response({
        "group": _group_to_dict(group), "trackedMembers": int(member_count),
        "security": {
            "antiSpam": bool(security.anti_spam) if security else False,
            "antiFlood": bool(security.anti_flood) if security else False,
            "emergencyMode": bool(security.emergency_mode) if security else False
        },
        "logs": [{
            "action": item.action,
            "details": json.dumps(item.details, ensure_ascii=False, default=str) if item.details is not None else "",
            "createdAt": item.created_at.isoformat() if item.created_at else None,
            "userId": str(item.user_id) if item.user_id else None
        } for item in logs]
    }, headers={"Cache-Control": "no-store"})



@require_admin
async def owner_group_permissions_handler(request):
    """Return the bot's current Telegram administrator permissions for one registered group."""
    bot = None
    try:
        group_id = int(request.match_info["group_id"])
        async with async_session_maker() as session:
            group = await session.get(Group, group_id)
            if not group or not group.is_active:
                return web.json_response({"error": "Active group not found."}, status=404)
        from bot.config import settings
        from aiogram import Bot
        bot = Bot(token=settings.BOT_TOKEN)
        me = await bot.get_me()
        member = await bot.get_chat_member(chat_id=group_id, user_id=me.id)
        status = str(getattr(member.status, "value", member.status))
        creator = status == "creator"
        admin = status == "administrator"
        perms = {
            "isAdministrator": admin or creator,
            "canDeleteMessages": creator or bool(getattr(member, "can_delete_messages", False)),
            "canRestrictMembers": creator or bool(getattr(member, "can_restrict_members", False)),
            "canInviteUsers": creator or bool(getattr(member, "can_invite_users", False)),
            "canPinMessages": creator or bool(getattr(member, "can_pin_messages", False)),
            "canPromoteMembers": creator or bool(getattr(member, "can_promote_members", False)),
            "canChangeInfo": creator or bool(getattr(member, "can_change_info", False)),
            "canManageChat": creator or admin,
            "status": status,
        }
        return web.json_response({"groupId": str(group_id), "permissions": perms},
                                 headers={"Cache-Control": "no-store"})
    except (ValueError, TypeError):
        return web.json_response({"error": "Invalid group ID."}, status=400)
    except Exception:
        return web.json_response({"error": "Could not read Telegram permissions. Check bot membership and connectivity."}, status=502)
    finally:
        if bot is not None:
            await bot.session.close()


@require_admin
async def owner_group_action_handler(request):
    """Perform a small set of Telegram actions only after checking live bot rights."""
    bot = None
    try:
        group_id = int(request.match_info["group_id"])
        action = request.match_info["action"]
        body = await request.json()
        target_id = int(body.get("userId", 0) or 0)
        message_id = int(body.get("messageId", 0) or 0)
        seconds = min(30 * 24 * 60 * 60, max(60, int(body.get("seconds", 3600) or 3600)))
        if action not in ("mute", "unmute", "ban", "unban", "pin", "invite"):
            return web.json_response({"error": "Unsupported group action."}, status=400)
        if action in ("mute", "unmute", "ban", "unban") and target_id <= 0:
            return web.json_response({"error": "A valid target user ID is required."}, status=400)
        if action == "pin" and message_id <= 0:
            return web.json_response({"error": "A valid Telegram message ID is required."}, status=400)
        async with async_session_maker() as session:
            group = await session.get(Group, group_id)
            if not group or not group.is_active:
                return web.json_response({"error": "Active group not found."}, status=404)
        from bot.config import settings
        from aiogram import Bot
        from aiogram.types import ChatPermissions
        from datetime import timedelta
        bot = Bot(token=settings.BOT_TOKEN)
        me = await bot.get_me()
        member = await bot.get_chat_member(chat_id=group_id, user_id=me.id)
        status = str(getattr(member.status, "value", member.status))
        creator = status == "creator"
        if status not in ("administrator", "creator"):
            return web.json_response({"error": "ALEXA must be a group administrator."}, status=403)
        if action in ("mute", "unmute", "ban", "unban"):
            if not creator and not getattr(member, "can_restrict_members", False):
                return web.json_response({"error": "ALEXA lacks Restrict Members permission."}, status=403)
            target = await bot.get_chat_member(chat_id=group_id, user_id=target_id)
            target_status = str(getattr(target.status, "value", target.status))
            if target_status in ("administrator", "creator"):
                return web.json_response({"error": "Telegram does not allow this action on group administrators."}, status=409)
            if action == "mute":
                await bot.restrict_chat_member(
                    chat_id=group_id, user_id=target_id,
                    permissions=ChatPermissions(can_send_messages=False, can_send_audios=False,
                        can_send_documents=False, can_send_photos=False, can_send_videos=False,
                        can_send_video_notes=False, can_send_voice_notes=False,
                        can_send_polls=False, can_send_other_messages=False,
                        can_add_web_page_previews=False),
                    until_date=timedelta(seconds=seconds),
                )
            elif action == "unmute":
                await bot.restrict_chat_member(
                    chat_id=group_id, user_id=target_id,
                    permissions=ChatPermissions(can_send_messages=True, can_send_audios=True,
                        can_send_documents=True, can_send_photos=True, can_send_videos=True,
                        can_send_video_notes=True, can_send_voice_notes=True,
                        can_send_polls=True, can_send_other_messages=True,
                        can_add_web_page_previews=True),
                )
            elif action == "ban":
                await bot.ban_chat_member(chat_id=group_id, user_id=target_id)
            else:
                await bot.unban_chat_member(chat_id=group_id, user_id=target_id, only_if_banned=True)
        elif action == "pin":
            if not creator and not getattr(member, "can_pin_messages", False):
                return web.json_response({"error": "ALEXA lacks Pin Messages permission."}, status=403)
            await bot.pin_chat_message(chat_id=group_id, message_id=message_id, disable_notification=True)
        elif action == "invite":
            if not creator and not getattr(member, "can_invite_users", False):
                return web.json_response({"error": "ALEXA lacks Invite Users permission."}, status=403)
            invite = await bot.create_chat_invite_link(chat_id=group_id, creates_join_request=False)
            return web.json_response({"success": True, "inviteLink": invite.invite_link})
        return web.json_response({"success": True, "action": action})
    except (ValueError, TypeError):
        return web.json_response({"error": "Invalid action parameters."}, status=400)
    except Exception:
        return web.json_response({"error": "Telegram rejected this action or the target is unavailable."}, status=502)
    finally:
        if bot is not None:
            await bot.session.close()


def setup_admin_routes(app):
    app.router.add_get('/owner', owner_app_handler)
    app.router.add_get('/owner-api/overview', owner_overview_handler)
    app.router.add_get('/owner-api/security', owner_security_status)
    app.router.add_get('/owner-api/system-controls', owner_system_controls_handler)
    app.router.add_post('/owner-api/system-controls', owner_system_controls_update_handler)
    app.router.add_get('/owner-api/referrals', owner_referrals_handler)
    app.router.add_post('/owner-api/referrals/requests/{request_id}', owner_affiliate_request_update)
    app.router.add_get('/owner-api/promotion-requests', owner_promotion_requests_handler)
    app.router.add_post('/owner-api/promotion-requests/{request_id}', owner_promotion_request_update)
    app.router.add_get('/owner-api/inbox', owner_inbox_handler)
    app.router.add_get('/owner-api/inbox/{user_id}/messages', owner_inbox_messages_handler)
    app.router.add_get('/owner-api/inbox/{user_id}/messages/{record_id}/media', owner_inbox_media_handler)
    app.router.add_get('/owner-api/groups/{group_id}/messages/{record_id}/media', owner_group_media_handler)
    app.router.add_get('/owner-api/avatar/{user_id}', owner_avatar_handler)
    app.router.add_get('/owner-api/group-avatar/{group_id}', owner_group_avatar_handler)
    app.router.add_get('/owner-api/groups/{group_id}/members', owner_group_members_handler)
    app.router.add_post('/owner-api/inbox/{user_id}/messages/{message_id}/delete', owner_inbox_delete_message_handler)
    app.router.add_get('/owner-api/groups/{group_id}/messages', owner_group_messages_handler)
    app.router.add_post('/owner-api/groups/{group_id}/messages/{message_id}/delete', owner_group_delete_message_handler)
    app.router.add_post('/owner-api/inbox/{user_id}/reply', owner_inbox_reply_handler)
    app.router.add_get('/owner-api/groups/{group_id}/activity', owner_group_activity_handler)
    app.router.add_get('/owner-api/groups/{group_id}/permissions', owner_group_permissions_handler)
    app.router.add_post('/owner-api/groups/{group_id}/actions/{action}', owner_group_action_handler)
    app.router.add_get('/owner-api/statistics', owner_statistics_handler)
    app.router.add_get('/owner-api/health', owner_health_handler)
    app.router.add_post('/owner-api/group-message', owner_group_message_handler)
    app.router.add_post('/owner-api/security', owner_security_update)
    # Reuse existing admin handlers behind Telegram owner-session verification.
    app.router.add_get('/owner-api/users', users_handler)
    app.router.add_post('/owner-api/users/{user_id}/ban', ban_user_handler)
    app.router.add_post('/owner-api/users/{user_id}/unban', unban_user_handler)
    app.router.add_get('/owner-api/groups', groups_handler)
    app.router.add_get('/owner-api/logs', logs_handler)
    app.router.add_post('/owner-api/broadcast', owner_dm_broadcast_handler)
    app.router.add_get('/owner-api/economy/leaderboard', economy_leaderboard_handler)
    app.router.add_get("/admin/stats", stats_handler)
    app.router.add_get("/admin/users", users_handler)
    app.router.add_post("/admin/users/{user_id}/ban", ban_user_handler)
    app.router.add_post("/admin/users/{user_id}/unban", unban_user_handler)
    app.router.add_get("/admin/groups", groups_handler)
    app.router.add_get("/admin/economy/leaderboard", economy_leaderboard_handler)
    app.router.add_get("/admin/logs", logs_handler)
    app.router.add_post("/admin/broadcast", broadcast_handler)
    app.router.add_get("/admin/twa/profile", twa_profile_handler)
    app.router.add_get("/admin/twa/leaderboard", twa_leaderboard_handler)
    app.router.add_post("/admin/twa/claim", twa_claim_handler)
    app.router.add_get("/admin/twa/game-stats", twa_game_stats_handler)
