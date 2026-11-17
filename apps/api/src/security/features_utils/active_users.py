"""Monthly member activity counts for organization analytics."""
from datetime import date, datetime, timezone
from sqlmodel import select, func
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlalchemy import and_
from src.db.user_activity import UserActivityDay
from src.db.user_organizations import UserOrganization
ACTIVE_DAYS_THRESHOLD = 2

def _month_bounds(year: int, month: int) -> tuple[date, date]:
    """Return (start inclusive, end exclusive) UTC dates for a calendar month."""
    start = date(year, month, 1)
    if month == 12:
        end = date(year + 1, 1, 1)
    else:
        end = date(year, month + 1, 1)
    return (start, end)

def current_utc_year_month() -> tuple[int, int]:
    now = datetime.now(timezone.utc)
    return (now.year, now.month)

async def count_active_users(org_id: int, year: int, month: int, db_session: AsyncSession) -> int:
    """Number of members with >= ACTIVE_DAYS_THRESHOLD distinct activity days in the month.

    Only current members count, so stray rows for non-members (whoever they
    came from) cannot inflate an org's figure.
    """
    start, end = _month_bounds(year, month)
    per_user = select(UserActivityDay.user_id).join(UserOrganization, and_(UserOrganization.user_id == UserActivityDay.user_id, UserOrganization.org_id == UserActivityDay.org_id)).where(UserActivityDay.org_id == org_id, UserActivityDay.activity_date >= start, UserActivityDay.activity_date < end).group_by(UserActivityDay.user_id).having(func.count(UserActivityDay.activity_date) >= ACTIVE_DAYS_THRESHOLD).subquery()
    result = await db_session.execute(select(func.count()).select_from(per_user))
    return int(result.scalar_one())

async def get_member_active_status(org_id: int, year: int, month: int, db_session: AsyncSession) -> dict[int, dict]:
    """
    Per-member visit count + active flag for the month, keyed by user_id.

    LEFT JOIN from org membership so members with zero visits still appear
    (visit_days=0, is_active=False).
    """
    start, end = _month_bounds(year, month)
    visit_days = func.count(UserActivityDay.id)
    stmt = select(UserOrganization.user_id, visit_days.label('visit_days')).select_from(UserOrganization).outerjoin(UserActivityDay, and_(UserActivityDay.org_id == UserOrganization.org_id, UserActivityDay.user_id == UserOrganization.user_id, UserActivityDay.activity_date >= start, UserActivityDay.activity_date < end)).where(UserOrganization.org_id == org_id).group_by(UserOrganization.user_id)
    rows = (await db_session.execute(stmt)).all()
    return {int(user_id): {'visit_days': int(days), 'is_active': int(days) >= ACTIVE_DAYS_THRESHOLD} for user_id, days in rows}

async def get_visit_days_for_users(org_id: int, user_ids: list[int], year: int, month: int, db_session: AsyncSession) -> dict[int, int]:
    """Distinct activity-day count in the month for a specific set of users."""
    if not user_ids:
        return {}
    start, end = _month_bounds(year, month)
    stmt = select(UserActivityDay.user_id, func.count(UserActivityDay.id)).where(UserActivityDay.org_id == org_id, UserActivityDay.user_id.in_(user_ids), UserActivityDay.activity_date >= start, UserActivityDay.activity_date < end).group_by(UserActivityDay.user_id)
    rows = (await db_session.execute(stmt)).all()
    return {int(uid): int(days) for uid, days in rows}
