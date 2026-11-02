"""Community Shield: opt-in, cross-group scam reports with human review only.

Reports are scoped to Telegram user IDs and group IDs. A target is only marked
community-flagged after reports from at least three distinct participating
groups. This service never bans or moderates a user automatically.
"""
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models import CommunityShieldGroup, CommunityShieldReport

COMMUNITY_FLAG_THRESHOLD = 3


async def set_group_enabled(session: AsyncSession, group_id: int, enabled: bool) -> bool:
    row = await session.get(CommunityShieldGroup, group_id)
    if row is None:
        candidate = CommunityShieldGroup(group_id=group_id, enabled=enabled)
        try:
            async with session.begin_nested():
                session.add(candidate)
                await session.flush()
            row = candidate
        except IntegrityError:
            row = await session.get(CommunityShieldGroup, group_id)
            if row is None:
                raise
            row.enabled = enabled
    else:
        row.enabled = enabled
    await session.flush()
    return row.enabled


async def group_is_enabled(session: AsyncSession, group_id: int) -> bool:
    row = await session.get(CommunityShieldGroup, group_id)
    return bool(row and row.enabled)


async def report_user(
    session: AsyncSession,
    *,
    group_id: int,
    target_user_id: int,
    reporter_user_id: int,
    reason: str,
) -> bool:
    """Save one report per target per group; return False for duplicate reports."""
    if not await group_is_enabled(session, group_id):
        raise ValueError("Community Shield is not enabled in this group.")
    existing = await session.scalar(
        select(CommunityShieldReport.id).where(
            CommunityShieldReport.group_id == group_id,
            CommunityShieldReport.target_user_id == target_user_id,
        )
    )
    if existing is not None:
        return False
    try:
        async with session.begin_nested():
            session.add(
                CommunityShieldReport(
                    group_id=group_id,
                    target_user_id=target_user_id,
                    reporter_user_id=reporter_user_id,
                    reason=reason[:500],
                )
            )
            await session.flush()
    except IntegrityError:
        # One report per group and target; concurrent duplicate is harmless.
        return False
    return True


async def report_summary(session: AsyncSession, target_user_id: int) -> tuple[int, bool]:
    """Count distinct reports only from currently participating groups."""
    count = await session.scalar(
        select(func.count(func.distinct(CommunityShieldReport.group_id)))
        .join(
            CommunityShieldGroup,
            CommunityShieldGroup.group_id == CommunityShieldReport.group_id,
        )
        .where(
            CommunityShieldGroup.enabled.is_(True),
            CommunityShieldReport.target_user_id == target_user_id,
        )
    )
    total = int(count or 0)
    return total, total >= COMMUNITY_FLAG_THRESHOLD
