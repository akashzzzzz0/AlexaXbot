"""Shared scoring and question bank for Alexa World Arena.

All Arena XP is virtual, global per Telegram user, and recorded in an append-only
score ledger so repeated callbacks cannot award the same event twice.
"""
from datetime import datetime, timezone
import random

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models import ArenaProfile, ArenaScoreEvent

QUESTION_BANK = [
    {"prompt": "Which planet is known as the Red Planet?", "options": ["Venus", "Mars", "Jupiter", "Mercury"], "answer": 1, "kind": "quiz"},
    {"prompt": "How many sides does a hexagon have?", "options": ["5", "6", "7", "8"], "answer": 1, "kind": "quiz"},
    {"prompt": "Which ocean is the largest?", "options": ["Atlantic", "Indian", "Arctic", "Pacific"], "answer": 3, "kind": "quiz"},
    {"prompt": "What is the chemical symbol for gold?", "options": ["Ag", "Gd", "Au", "Go"], "answer": 2, "kind": "quiz"},
    {"prompt": "Which language is primarily used to style web pages?", "options": ["HTML", "CSS", "SQL", "Python"], "answer": 1, "kind": "quiz"},
    {"prompt": "What is 9 × 7?", "options": ["56", "63", "72", "49"], "answer": 1, "kind": "puzzle"},
    {"prompt": "What comes next: 2, 4, 8, 16, ...?", "options": ["18", "24", "30", "32"], "answer": 3, "kind": "puzzle"},
    {"prompt": "I have keys but no locks, and space but no room. What am I?", "options": ["A map", "A keyboard", "A clock", "A book"], "answer": 1, "kind": "puzzle"},
    {"prompt": "What is 144 ÷ 12?", "options": ["10", "11", "12", "14"], "answer": 2, "kind": "puzzle"},
    {"prompt": "Which word does not belong?", "options": ["Apple", "Banana", "Carrot", "Mango"], "answer": 2, "kind": "puzzle"},
    {"prompt": "How many minutes are in two hours?", "options": ["100", "110", "120", "140"], "answer": 2, "kind": "quiz"},
    {"prompt": "Which animal is a mammal?", "options": ["Shark", "Frog", "Dolphin", "Trout"], "answer": 2, "kind": "quiz"},
]


def rank_for_xp(xp: int) -> str:
    """Stable rank thresholds for global Arena XP."""
    if xp >= 700:
        return "Legend"
    if xp >= 300:
        return "Elite"
    if xp >= 100:
        return "Warrior"
    return "Rookie"


def choose_question(kind: str = "mixed") -> dict:
    pool = QUESTION_BANK
    if kind in {"quiz", "puzzle"}:
        filtered = [q for q in QUESTION_BANK if q["kind"] == kind]
        if filtered:
            pool = filtered
    item = random.choice(pool)
    return {"prompt": item["prompt"], "options": list(item["options"]), "answer": item["answer"], "kind": item["kind"]}


def utc_day_key(now: datetime | None = None) -> str:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).date().isoformat()


def calculate_battle_rewards(challenger_correct: bool, opponent_correct: bool,
                             challenger_time: float, opponent_time: float) -> tuple[int, int, int, int]:
    """Return (challenger XP, opponent XP, challenger wins, opponent wins)."""
    if challenger_correct and opponent_correct:
        if challenger_time == opponent_time:
            return 10, 10, 0, 0
        return (20, 5, 1, 0) if challenger_time < opponent_time else (5, 20, 0, 1)
    if challenger_correct:
        return 20, 5, 1, 0
    if opponent_correct:
        return 5, 20, 0, 1
    return 2, 2, 0, 0


async def get_or_create_profile(session: AsyncSession, user_id: int) -> ArenaProfile:
    profile = await session.get(ArenaProfile, user_id)
    if profile is None:
        profile = ArenaProfile(user_id=user_id)
        session.add(profile)
        await session.flush()
    return profile


async def award_xp(session: AsyncSession, user_id: int, group_id: int | None,
                   mode: str, amount: int, event_key: str, wins: int = 0) -> bool:
    """Award a unique XP event. Returns False when this event was already recorded."""
    existing = await session.scalar(
        select(ArenaScoreEvent.id).where(ArenaScoreEvent.event_key == event_key)
    )
    if existing is not None:
        return False
    profile = await get_or_create_profile(session, user_id)
    session.add(ArenaScoreEvent(
        event_key=event_key,
        user_id=user_id,
        group_id=group_id,
        mode=mode,
        xp_delta=amount,
    ))
    profile.xp = int(profile.xp or 0) + amount
    profile.total_games = int(profile.total_games or 0) + 1
    profile.wins = int(profile.wins or 0) + wins
    badges = list(profile.badges or [])
    milestones = [
        (100, "first_century"),
        (500, "elite_challenger"),
        (1000, "world_legend"),
    ]
    for threshold, badge in milestones:
        if profile.xp >= threshold and badge not in badges:
            badges.append(badge)
    profile.badges = badges
    await session.flush()
    return True
