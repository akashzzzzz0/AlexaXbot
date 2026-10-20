"""Persistence and scoring rules for ALEXA Multiverse Wars."""
from datetime import date, datetime, timedelta, timezone
import random
import re

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models import (
    WarGuild, WarSeasonScore, WarContribution, WarChallenge,
    WarChallengeContribution, MysteryRewardClaim, Wallet,
)

MISSION_POINTS = 5
CHALLENGE_WIN_BONUS = 20
MAX_DAILY_ATTEMPTS = 3
CHALLENGE_HOURS = 24
_PENDING_HOURS = 48

_DAILY_QUESTIONS = (
    ("What is 12 × 7?", "84"),
    ("How many sides does a hexagon have?", "6"),
    ("What is 144 ÷ 12?", "12"),
    ("What is 15 + 27?", "42"),
    ("What is 9 squared?", "81"),
    ("What is 100 − 37?", "63"),
    ("What is 3 cubed?", "27"),
    ("How many minutes are in 2 hours?", "120"),
    ("Next number: 2, 4, 8, 16, ?", "32"),
    ("How many days are in a leap year?", "366"),
)


def season_key(now: datetime | None = None) -> str:
    """Use UTC ISO weeks so all participating groups share the same season."""
    moment = now or datetime.now(timezone.utc)
    return moment.strftime("%G-W%V")


def day_key(now: datetime | None = None) -> str:
    moment = now or datetime.now(timezone.utc)
    return moment.date().isoformat()


def daily_mission(now: datetime | None = None) -> tuple[str, str]:
    moment = now or datetime.now(timezone.utc)
    return _DAILY_QUESTIONS[moment.date().toordinal() % len(_DAILY_QUESTIONS)]


def normalize_answer(value: str) -> str:
    return re.sub(r"\s+", "", (value or "").strip().casefold())


async def register_guild(
    session: AsyncSession, *, group_id: int, title: str,
    username: str | None, admin_id: int,
) -> WarGuild:
    guild = await session.get(WarGuild, group_id)
    if guild is None:
        guild = WarGuild(
            group_id=group_id, title=(title or "Unnamed group")[:256],
            username=username, registered_by=admin_id, is_active=True,
        )
        session.add(guild)
    else:
        guild.title = (title or "Unnamed group")[:256]
        guild.username = username
        guild.registered_by = admin_id
        guild.is_active = True
    await session.flush()
    return guild


async def get_score(session: AsyncSession, group_id: int, key: str | None = None) -> WarSeasonScore:
    key = key or season_key()
    score = await session.scalar(
        select(WarSeasonScore).where(
            WarSeasonScore.group_id == group_id,
            WarSeasonScore.season_key == key,
        )
    )
    if score is None:
        score = WarSeasonScore(group_id=group_id, season_key=key, points=0, wins=0, draws=0, losses=0)
        session.add(score)
        await session.flush()
    return score


async def leaderboard(session: AsyncSession, key: str | None = None, limit: int = 10):
    key = key or season_key()
    return (await session.execute(
        select(WarGuild, WarSeasonScore)
        .join(WarSeasonScore, WarSeasonScore.group_id == WarGuild.group_id)
        .where(WarGuild.is_active.is_(True), WarSeasonScore.season_key == key)
        .order_by(WarSeasonScore.points.desc(), WarSeasonScore.wins.desc(), WarGuild.title.asc())
        .limit(max(1, min(limit, 20)))
    )).all()


async def submit_answer(
    session: AsyncSession, *, group_id: int, user_id: int, answer: str,
    now: datetime | None = None,
) -> dict:
    moment = now or datetime.now(timezone.utc)
    guild = await session.get(WarGuild, group_id)
    if guild is None or not guild.is_active:
        return {"status": "not_registered"}
    today = day_key(moment)
    question, expected = daily_mission(moment)
    contribution = await session.scalar(
        select(WarContribution).where(
            WarContribution.group_id == group_id,
            WarContribution.user_id == user_id,
            WarContribution.day_key == today,
        )
    )
    if contribution is None:
        contribution = WarContribution(
            group_id=group_id, user_id=user_id, day_key=today,
            attempts=0, completed=False, points=0,
        )
        session.add(contribution)
        await session.flush()
    if contribution.completed:
        return {"status": "already_completed", "points": contribution.points}
    if contribution.attempts >= MAX_DAILY_ATTEMPTS:
        return {"status": "attempts_exhausted", "attempts": contribution.attempts}

    contribution.attempts += 1
    if normalize_answer(answer) != normalize_answer(expected):
        await session.flush()
        return {
            "status": "incorrect",
            "attempts": contribution.attempts,
            "remaining": MAX_DAILY_ATTEMPTS - contribution.attempts,
        }

    contribution.completed = True
    contribution.points = MISSION_POINTS
    score = await get_score(session, group_id, season_key(moment))
    score.points = (score.points or 0) + MISSION_POINTS

    # Each member contributes at most once to each active cross-group challenge.
    challenges = (await session.execute(
        select(WarChallenge).where(
            WarChallenge.status == "active",
            WarChallenge.ends_at > moment,
            or_(
                WarChallenge.challenger_group_id == group_id,
                WarChallenge.opponent_group_id == group_id,
            ),
        )
    )).scalars().all()
    challenge_points = 0
    for challenge in challenges:
        already_counted = await session.scalar(
            select(WarChallengeContribution.id).where(
                WarChallengeContribution.challenge_id == challenge.id,
                WarChallengeContribution.group_id == group_id,
                WarChallengeContribution.user_id == user_id,
            )
        )
        if already_counted is not None:
            continue
        session.add(WarChallengeContribution(
            challenge_id=challenge.id, group_id=group_id, user_id=user_id,
        ))
        if challenge.challenger_group_id == group_id:
            challenge.challenger_points = (challenge.challenger_points or 0) + 1
        else:
            challenge.opponent_points = (challenge.opponent_points or 0) + 1
        challenge_points += 1
    await session.flush()
    return {
        "status": "correct", "points": MISSION_POINTS,
        "season_points": score.points, "challenge_points": challenge_points,
        "question": question,
    }


async def create_challenge(
    session: AsyncSession, *, challenger_group_id: int,
    opponent_group_id: int, created_by: int,
    now: datetime | None = None,
) -> WarChallenge:
    moment = now or datetime.now(timezone.utc)
    if challenger_group_id == opponent_group_id:
        raise ValueError("A group cannot challenge itself.")
    challenger = await session.get(WarGuild, challenger_group_id)
    opponent = await session.get(WarGuild, opponent_group_id)
    if not challenger or not challenger.is_active:
        raise ValueError("Register this group first with /warregister.")
    if not opponent or not opponent.is_active:
        raise ValueError("The target group is not registered for Multiverse Wars.")
    pending = await session.scalar(
        select(WarChallenge.id).where(
            WarChallenge.status.in_(["pending", "active"]),
            or_(
                and_(WarChallenge.challenger_group_id == challenger_group_id,
                     WarChallenge.opponent_group_id == opponent_group_id),
                and_(WarChallenge.challenger_group_id == opponent_group_id,
                     WarChallenge.opponent_group_id == challenger_group_id),
            ),
        )
    )
    if pending is not None:
        raise ValueError("A challenge between these groups is already pending or active.")
    challenge = WarChallenge(
        challenger_group_id=challenger_group_id,
        opponent_group_id=opponent_group_id,
        season_key=season_key(moment), status="pending",
        created_by=created_by, challenger_points=0, opponent_points=0,
    )
    session.add(challenge)
    await session.flush()
    return challenge


async def accept_challenge(
    session: AsyncSession, *, challenge_id: int, group_id: int,
    admin_id: int, now: datetime | None = None,
) -> WarChallenge:
    moment = now or datetime.now(timezone.utc)
    challenge = await session.get(WarChallenge, challenge_id)
    if not challenge or challenge.status != "pending":
        raise ValueError("Pending challenge not found.")
    if challenge.opponent_group_id != group_id:
        raise ValueError("Only the challenged group can accept this challenge.")
    created = challenge.created_at
    if created and created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    if created and created < moment - timedelta(hours=_PENDING_HOURS):
        challenge.status = "expired"
        await session.flush()
        raise ValueError("This challenge expired after 48 hours.")
    challenge.status = "active"
    challenge.accepted_by = admin_id
    challenge.accepted_at = moment
    challenge.ends_at = moment + timedelta(hours=CHALLENGE_HOURS)
    await session.flush()
    return challenge


async def settle_expired_challenges(session: AsyncSession, now: datetime | None = None) -> int:
    moment = now or datetime.now(timezone.utc)
    expired = (await session.execute(
        select(WarChallenge).where(
            WarChallenge.status == "active",
            WarChallenge.ends_at <= moment,
        )
    )).scalars().all()
    for challenge in expired:
        challenge.status = "completed"
        challenge.resolved_at = moment
        if challenge.challenger_points > challenge.opponent_points:
            winner, loser = challenge.challenger_group_id, challenge.opponent_group_id
        elif challenge.opponent_points > challenge.challenger_points:
            winner, loser = challenge.opponent_group_id, challenge.challenger_group_id
        else:
            winner = loser = None
        challenge.winner_group_id = winner
        left = await get_score(session, challenge.challenger_group_id, challenge.season_key)
        right = await get_score(session, challenge.opponent_group_id, challenge.season_key)
        if winner is None:
            left.draws = (left.draws or 0) + 1
            right.draws = (right.draws or 0) + 1
        else:
            winning_score = left if winner == challenge.challenger_group_id else right
            losing_score = right if winner == challenge.challenger_group_id else left
            winning_score.wins = (winning_score.wins or 0) + 1
            winning_score.points = (winning_score.points or 0) + CHALLENGE_WIN_BONUS
            losing_score.losses = (losing_score.losses or 0) + 1

    stale_pending = (await session.execute(
        select(WarChallenge).where(
            WarChallenge.status == "pending",
            WarChallenge.created_at <= moment - timedelta(hours=_PENDING_HOURS),
        )
    )).scalars().all()
    for challenge in stale_pending:
        challenge.status = "expired"
        challenge.resolved_at = moment
    await session.flush()
    return len(expired) + len(stale_pending)



async def world_leaderboard(session: AsyncSession, limit: int = 10):
    """All-time ranking across every season, not just the current week."""
    total_points = func.coalesce(func.sum(WarSeasonScore.points), 0).label("total_points")
    return (await session.execute(
        select(WarGuild, total_points)
        .outerjoin(WarSeasonScore, WarSeasonScore.group_id == WarGuild.group_id)
        .where(WarGuild.is_active.is_(True))
        .group_by(
            WarGuild.group_id, WarGuild.title, WarGuild.username,
            WarGuild.registered_by, WarGuild.is_active, WarGuild.registered_at,
        )
        .order_by(total_points.desc(), WarGuild.title.asc())
        .limit(max(1, min(limit, 20)))
    )).all()


async def claim_mystery_reward(
    session: AsyncSession, *, user_id: int, now: datetime | None = None,
) -> dict:
    """Award a small virtual-coin surprise once per UTC day."""
    moment = now or datetime.now(timezone.utc)
    today = day_key(moment)
    existing = await session.scalar(
        select(MysteryRewardClaim).where(
            MysteryRewardClaim.user_id == user_id,
            MysteryRewardClaim.day_key == today,
        )
    )
    if existing is not None:
        return {"status": "already_claimed", "amount": existing.reward_amount}

    amount = random.choice((25, 50, 75, 100, 150, 250))
    wallet = await session.scalar(select(Wallet).where(Wallet.user_id == user_id))
    if wallet is None:
        wallet = Wallet(user_id=user_id, balance=0, bank_balance=0, total_earned=0, total_spent=0)
        session.add(wallet)
        await session.flush()
    wallet.balance = (wallet.balance or 0) + amount
    wallet.total_earned = (wallet.total_earned or 0) + amount
    session.add(MysteryRewardClaim(user_id=user_id, day_key=today, reward_amount=amount))
    await session.flush()
    return {"status": "claimed", "amount": amount, "balance": wallet.balance}
