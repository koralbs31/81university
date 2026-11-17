from typing import Any, List, Optional, Sequence
from fastapi import Request
from sqlalchemy import ColumnElement, func, true as sa_true
from sqlmodel import select, or_, and_
from sqlmodel.ext.asyncio.session import AsyncSession
from pydantic import BaseModel, ConfigDict
from src.db.users import PublicUser, AnonymousUser, UserReadPublic, User, APITokenUser
from src.db.courses.courses import Course, CourseRead
from src.db.courses.activities import Activity, ActivityLockType
from src.db.courses.chapters import Chapter, LockType as ChapterLockType
from src.db.courses.chapter_activities import ChapterActivity
from src.db.organizations import Organization
from src.db.user_organizations import UserOrganization
from src.db.communities.communities import Community, CommunityRead
from src.db.communities.discussions import Discussion, DiscussionRead
from src.services.courses.courses import accessible_courses_filter, search_courses
from src.services.search.normalization import LIKE_ESCAPE_CHAR, build_like_patterns, escape_like_wildcards
from src.security.api_token_utils import require_token_content_read, token_has_right
from src.security.auth import resolve_acting_user_id
from src.services.orgs.auth_policy import viewer_for_org
from src.security.org_auth import is_org_admin, is_org_member
from src.security.superadmin import is_user_superadmin
from src.services.communities.communities import accessible_community_ids_select, can_read_all_communities, community_read

class SearchDiscussionRead(DiscussionRead):
    """DiscussionRead plus the parent community UUID so the UI can deep-link
    to `/community/{uuid}/discussion/{uuid}` without another round trip."""
    community_uuid: str = ''

class SearchActivityRead(BaseModel):
    """A lesson hit, with enough of its course and chapter for the UI to link
    to `/course/{uuid}/activity/{uuid}` and label where the lesson lives.

    Deliberately not ``ActivityRead``: that carries the lesson body, and a
    search row must never leak content the activity read itself would gate
    (paid, locked). Names only.
    """
    activity_uuid: str
    name: str
    activity_type: str
    activity_sub_type: str
    course_uuid: str
    course_name: str
    course_thumbnail_image: Optional[str] = None
    chapter_uuid: str = ''
    chapter_name: str = ''

class SearchResult(BaseModel):
    """Paginated search result grouped by resource type.

    Each resource list is capped at `limit` per page; `total_*` holds the full
    count for the whole query so the UI can show per-tab totals.
    """
    model_config = ConfigDict(arbitrary_types_allowed=True)
    courses: List[CourseRead]
    activities: List[SearchActivityRead] = []
    users: List[UserReadPublic]
    communities: List[CommunityRead]
    discussions: List[SearchDiscussionRead]
    total_courses: int = 0
    total_activities: int = 0
    total_users: int = 0
    total_communities: int = 0
    total_discussions: int = 0

def _empty_result() -> SearchResult:
    return SearchResult(courses=[], activities=[], users=[], communities=[], discussions=[])
_escape_like_wildcards = escape_like_wildcards

def _token_reads_content(current_user: APITokenUser, resource: str) -> bool:
    """``require_token_content_read`` as a yes/no, for filtering results."""
    from fastapi import HTTPException
    try:
        require_token_content_read(current_user, resource)
    except HTTPException:
        return False
    return True

def _ilike_any(columns: Sequence[ColumnElement[Any]], pattern: str) -> ColumnElement[bool]:
    """Return a SQL boolean matching `pattern` against any of the given columns.

    Uses SQLAlchemy's `ilike`, which is parameterized and keeps the driver
    responsible for escaping; no string interpolation on user input.
    """
    return or_(*(column.ilike(pattern, escape=LIKE_ESCAPE_CHAR) for column in columns))

def _ilike_terms(columns: Sequence[ColumnElement[Any]], patterns: Sequence[str]) -> ColumnElement[bool]:
    """Every pattern must match at least one of the columns.

    This is the keyword behaviour: each word of the query has to show up
    somewhere in the row, in any order and in any of the searched fields, so
    "python intro" finds a course named "Introduction to Python". With a
    single term it is exactly the old whole-query substring match. With no
    terms it matches everything, which the router's ``min_length`` rules out.
    """
    if not patterns:
        return sa_true()
    return and_(*(_ilike_any(columns, pattern) for pattern in patterns))

async def _paginate_and_count(db_session: AsyncSession, query, page: int, limit: int) -> tuple[list, int]:
    """Run `query` with LIMIT/OFFSET for the current page and return both the
    page rows (as scalar model instances) and the full unpaginated count in one
    helper.

    Note: only use this helper for single-model selects (e.g. ``select(Foo)``).
    Multi-column selects (e.g. ``select(Foo, Bar.col)``) return tuples and must
    be fetched with ``.all()`` directly.
    """
    offset = (page - 1) * limit
    rows = (await db_session.execute(query.offset(offset).limit(limit))).scalars().all()
    total = (await db_session.execute(select(func.count()).select_from(query.order_by(None).subquery()))).scalar_one()
    return (list(rows), int(total))

async def _paginate_and_count_rows(db_session: AsyncSession, query, page: int, limit: int) -> tuple[list, int]:
    """Like ``_paginate_and_count`` but preserves multi-column ``Row`` tuples.

    Use this for selects that return more than one column, e.g.
    ``select(Discussion, Community.community_uuid)``.
    """
    offset = (page - 1) * limit
    rows = (await db_session.execute(query.offset(offset).limit(limit))).all()
    total = (await db_session.execute(select(func.count()).select_from(query.order_by(None).subquery()))).scalar_one()
    return (list(rows), int(total))

async def search_across_org(request: Request, current_user: PublicUser | AnonymousUser | APITokenUser, org_slug: str, search_query: str, db_session: AsyncSession, page: int=1, limit: int=10) -> SearchResult:
    """Search across the org's resources with per-type access control.

    SECURITY:
    - Anonymous users see public content only; they cannot search users.
    - Authenticated non-members see public content only; they cannot search users.
    - Org members additionally see org-scoped non-public content where the
      resource itself doesn't restrict it further (e.g. unpublished items and
      usergroup-restricted playgrounds are always excluded from search).
      Usergroup-restricted communities (and their discussions) and podcasts
      only show up for members of a linked group, as in their listings.
    - User hits are serialized as ``UserReadPublic``, never ``UserRead``: search
      is open to every member, so it must not expose the PII the dedicated
      member directory restricts to admins.
    - Pattern matching goes through SQLAlchemy `ilike`, which is fully parameterized.
    - Limit is capped at 50 per page.
    """
    from fastapi import HTTPException, status
    limit = min(limit, 50)
    patterns = build_like_patterns(search_query)
    org = (await db_session.execute(select(Organization).where(Organization.slug == org_slug))).scalars().first()
    if not org:
        return _empty_result()
    if isinstance(current_user, APITokenUser):
        if org.id != current_user.org_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='API token cannot search in organizations outside its scope')
        if not token_has_right(current_user, 'search', 'action_read'):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='API token does not have search permission')
    current_user = await viewer_for_org(db_session, current_user, org.id)
    is_anon = isinstance(current_user, AnonymousUser)
    is_token = isinstance(current_user, APITokenUser)
    user_is_member = not is_anon and await is_org_member(resolve_acting_user_id(current_user), org.id, db_session)
    only_public = is_anon or not user_is_member
    acting_user_id = 0 if is_anon else resolve_acting_user_id(current_user)
    if isinstance(current_user, APITokenUser) and (not token_has_right(current_user, 'courses', 'action_read')):
        courses = []
    else:
        courses = await search_courses(request, current_user, org_slug, search_query, db_session, page, limit)
    total_courses = len(courses)
    if isinstance(current_user, APITokenUser) and (not token_has_right(current_user, 'courses', 'action_read')):
        activities: list = []
        total_activities = 0
    else:
        is_superadmin = not is_anon and await is_user_superadmin(acting_user_id, db_session)
        admin_for_locks = not is_anon and await is_org_admin(acting_user_id, org.id, db_session)
        activities_q = select(Activity, Course, Chapter).join(Course, Course.id == Activity.course_id).join(ChapterActivity, ChapterActivity.activity_id == Activity.id).join(Chapter, Chapter.id == ChapterActivity.chapter_id).where(Activity.org_id == org.id).where(Activity.published == sa_true()).where(_ilike_terms([Activity.name], patterns)).where(accessible_courses_filter(acting_user_id, is_anonymous=is_anon, is_api_token=isinstance(current_user, APITokenUser), is_superadmin=is_superadmin))
        if not admin_for_locks:
            activity_locks = [ActivityLockType.PUBLIC]
            chapter_locks = [ChapterLockType.PUBLIC]
            if not is_anon:
                activity_locks.append(ActivityLockType.AUTHENTICATED)
                chapter_locks.append(ChapterLockType.AUTHENTICATED)
            activities_q = activities_q.where(Activity.lock_type.in_(activity_locks), or_(Chapter.lock_type.is_(None), Chapter.lock_type.in_(chapter_locks)))
        activities_q = activities_q.order_by(Course.name, ChapterActivity.order, Activity.name)
        activities, total_activities = await _paginate_and_count_rows(db_session, activities_q, page, limit)
    if only_public or (is_token and (not token_has_right(current_user, 'users', 'action_read'))):
        users: list = []
        total_users = 0
    else:
        users_q = select(User).join(UserOrganization, and_(UserOrganization.user_id == User.id, UserOrganization.org_id == org.id)).where(_ilike_terms([User.username, User.first_name, User.last_name], patterns))
        from src.services.demo.guards import hide_other_visitors
        if org.is_demo:
            users_q = hide_other_visitors(users_q, resolve_acting_user_id(current_user))
        users, total_users = await _paginate_and_count(db_session, users_q, page, limit)
    communities_q = select(Community).where(Community.org_id == org.id).where(_ilike_terms([Community.name, Community.description], patterns))
    community_filter = None
    if only_public or (is_token and (not _token_reads_content(current_user, 'communities'))):
        community_filter = Community.public == sa_true()
    elif is_token:
        pass
    elif not await can_read_all_communities(request, org, acting_user_id, db_session):
        community_filter = Community.id.in_(accessible_community_ids_select(org.id, acting_user_id))
    if community_filter is not None:
        communities_q = communities_q.where(community_filter)
    communities, total_communities = await _paginate_and_count(db_session, communities_q, page, limit)
    discussions_q = select(Discussion, Community.community_uuid).join(Community, Community.id == Discussion.community_id).where(Discussion.org_id == org.id).where(_ilike_terms([Discussion.title, Discussion.content], patterns))
    if community_filter is not None:
        discussions_q = discussions_q.where(community_filter)
    discussions, total_discussions = await _paginate_and_count_rows(db_session, discussions_q, page, limit)
    activity_reads = [SearchActivityRead(activity_uuid=activity.activity_uuid, name=activity.name, activity_type=str(getattr(activity.activity_type, 'value', activity.activity_type)), activity_sub_type=str(getattr(activity.activity_sub_type, 'value', activity.activity_sub_type)), course_uuid=course.course_uuid, course_name=course.name, course_thumbnail_image=course.thumbnail_image, chapter_uuid=chapter.chapter_uuid or '', chapter_name=chapter.name or '') for activity, course, chapter in activities]
    user_reads = [UserReadPublic.model_validate(u) for u in users]
    community_reads = [community_read(c) for c in communities]
    discussion_reads = [SearchDiscussionRead(**discussion.model_dump(), community_uuid=community_uuid or '') for discussion, community_uuid in discussions]
    return SearchResult(courses=courses, activities=activity_reads, users=user_reads, communities=community_reads, discussions=discussion_reads, total_courses=total_courses, total_activities=total_activities, total_users=total_users, total_communities=total_communities, total_discussions=total_discussions)
