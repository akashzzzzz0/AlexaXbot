from datetime import date
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from bot.database.models import Mission, UserMission

DEFAULT_MISSIONS = [
    {"code":"daily_game_1", "type":"daily", "name_key":"Play 1 game", "description_key":"Complete one game today", "icon":"🎮", "reward_coins":100, "reward_xp":20, "requirement_type":"games", "requirement_value":1},
    {"code":"daily_messages_10", "type":"daily", "name_key":"Send 10 messages", "description_key":"Send 10 non-command messages today", "icon":"💬", "reward_coins":75, "reward_xp":15, "requirement_type":"messages", "requirement_value":10},
    {"code":"weekly_game_10", "type":"weekly", "name_key":"Play 10 games", "description_key":"Complete 10 games this week", "icon":"🏆", "reward_coins":500, "reward_xp":100, "requirement_type":"games", "requirement_value":10},
    {"code":"monthly_game_30", "type":"monthly", "name_key":"Play 30 games", "description_key":"Complete 30 games this month", "icon":"🌟", "reward_coins":1500, "reward_xp":300, "requirement_type":"games", "requirement_value":30},
]

def period_key(period_type: str, now: date | None = None) -> str:
    now = now or date.today()
    if period_type == "daily":
        return now.isoformat()
    if period_type == "weekly":
        iso = now.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    return now.strftime("%Y-%m")

async def seed_missions(session: AsyncSession) -> None:
    for spec in DEFAULT_MISSIONS:
        exists = await session.scalar(select(Mission).where(Mission.code == spec["code"]))
        if not exists:
            session.add(Mission(**spec, is_active=True))
    await session.flush()

async def track_progress(session: AsyncSession, user_id: int, event_type: str) -> None:
    if event_type not in {"games", "messages"}:
        return
    missions = (await session.execute(select(Mission).where(Mission.is_active.is_(True), Mission.requirement_type == event_type))).scalars().all()
    for mission in missions:
        key = period_key(mission.type)
        row = await session.scalar(select(UserMission).where(UserMission.user_id == user_id, UserMission.mission_id == mission.id, UserMission.period == key))
        if row is None:
            row = UserMission(user_id=user_id, mission_id=mission.id, period=key, progress=0, completed=False)
            session.add(row)
            await session.flush()
        if row.completed:
            continue
        row.progress = (row.progress or 0) + 1
        row.completed = row.progress >= (mission.requirement_value or 1)
    await session.flush()
