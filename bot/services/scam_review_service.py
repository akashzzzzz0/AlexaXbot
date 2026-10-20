"""Admin-reviewed scam reports. Reports are evidence for human review, not proof."""
from datetime import datetime, timezone

from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.models import ScamReview


async def submit_scam_review(
    session: AsyncSession, *, group_id: int, target_user_id: int,
    reporter_user_id: int, reason: str, evidence: str,
) -> ScamReview:
    reason = " ".join((reason or "").split())
    if len(reason) < 8:
        raise ValueError("Please provide a reason of at least 8 characters.")
    if target_user_id == reporter_user_id:
        raise ValueError("You cannot report yourself.")
    item = ScamReview(
        group_id=group_id,
        target_user_id=target_user_id,
        reporter_user_id=reporter_user_id,
        reason=reason[:500],
        evidence=(evidence or "")[:1000],
        status="pending",
    )
    session.add(item)
    await session.flush()
    return item


async def list_pending_reviews(session: AsyncSession, group_id: int, limit: int = 10):
    result = await session.scalars(
        select(ScamReview)
        .where(ScamReview.group_id == group_id, ScamReview.status == "pending")
        .order_by(desc(ScamReview.created_at), desc(ScamReview.id))
        .limit(max(1, min(limit, 20)))
    )
    return list(result.all())


async def resolve_scam_review(
    session: AsyncSession, *, review_id: int, group_id: int,
    resolver_user_id: int, note: str = "",
) -> bool:
    item = await session.scalar(
        select(ScamReview).where(
            ScamReview.id == review_id,
            ScamReview.group_id == group_id,
            ScamReview.status == "pending",
        )
    )
    if item is None:
        return False
    item.status = "resolved"
    item.resolved_by = resolver_user_id
    item.resolution_note = " ".join((note or "").split())[:500] or None
    item.resolved_at = datetime.now(timezone.utc)
    await session.flush()
    return True
