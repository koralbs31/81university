from typing import List
from uuid import uuid4
import json
import logging
from sqlmodel import select, or_, and_, func
from sqlalchemy import true as sa_true
from sqlmodel.ext.asyncio.session import AsyncSession
from src.core.ee_hooks import notify_ee_course_content_changed
from src.db.usergroup_resources import UserGroupResource
from src.db.usergroup_user import UserGroupUser
from src.db.organizations import Organization
from src.db.organization_config import OrganizationConfig
from src.db.roles import Role
from src.db.user_organizations import UserOrganization
from src.security.features_utils.usage import check_limits_with_usage, decrease_feature_usage, increase_feature_usage
from src.db.resource_authors import ResourceAuthor, ResourceAuthorshipEnum, ResourceAuthorshipStatusEnum
from src.db.users import PublicUser, AnonymousUser, User, UserReadAuthor, APITokenUser
from src.db.courses.courses import Course, CourseCreate, CourseRead, CourseUpdate, FullCourseRead, AuthorWithRole, ThumbnailType
from src.security.api_token_utils import require_token_right
from src.security.auth import resolve_acting_user_id
from src.services.orgs.auth_policy import viewer_for_org
from src.security.org_auth import require_org_create_permission
from src.security.rbac.rbac import authorization_verify_if_user_is_anon, authorization_verify_based_on_org_admin_status
from src.security.rbac import AccessAction, AccessContext, check_resource_access
from src.security.rbac.constants import ADMIN_OR_MAINTAINER_ROLE_IDS
from src.security.superadmin import is_user_superadmin
from src.services.courses.thumbnails import upload_thumbnail
from src.services.search.normalization import LIKE_ESCAPE_CHAR, build_like_patterns
from src.services.webhooks.dispatch import dispatch_webhooks
from src.services.analytics import events as analytics_events
from src.services.analytics.analytics import track
from fastapi import HTTPException, Request, UploadFile, status
from datetime import datetime
logger = logging.getLogger(__name__)

def _is_bare_filename(value: str) -> bool:
    """Thumbnail names are joined onto the course's thumbnails dir, so they
    must be a bare file name: no separators, traversal, NUL or absolute path."""
    if not value or '\x00' in value:
        return False
    if '/' in value or '\\' in value:
        return False
    return value not in ('.', '..') and (not value.startswith('.'))

async def get_course(request: Request, course_uuid: str, current_user: PublicUser | AnonymousUser, db_session: AsyncSession):
    statement = select(Course).where(Course.course_uuid == course_uuid)
    course = (await db_session.execute(statement)).scalars().first()
    if not course:
        raise HTTPException(status_code=404, detail='Course not found')
    await check_resource_access(request, db_session, current_user, course.course_uuid, AccessAction.READ, context=AccessContext.DASHBOARD)
    authors_statement = select(ResourceAuthor, User).join(User, ResourceAuthor.user_id == User.id).where(ResourceAuthor.resource_uuid == course.course_uuid).order_by(ResourceAuthor.id.asc())
    author_results = (await db_session.execute(authors_statement)).all()
    authors = [AuthorWithRole(user=UserReadAuthor.model_validate(user), authorship=resource_author.authorship, authorship_status=resource_author.authorship_status, creation_date=resource_author.creation_date, update_date=resource_author.update_date) for resource_author, user in author_results]
    course = CourseRead(**course.model_dump(), authors=authors)
    return course

async def get_course_by_id(request: Request, course_id: int, current_user: PublicUser | AnonymousUser, db_session: AsyncSession):
    statement = select(Course).where(Course.id == course_id)
    course = (await db_session.execute(statement)).scalars().first()
    if not course:
        raise HTTPException(status_code=404, detail='Course not found')
    await check_resource_access(request, db_session, current_user, course.course_uuid, AccessAction.READ)
    authors_statement = select(ResourceAuthor, User).join(User, ResourceAuthor.user_id == User.id).where(ResourceAuthor.resource_uuid == course.course_uuid).order_by(ResourceAuthor.id.asc())
    author_results = (await db_session.execute(authors_statement)).all()
    authors = [AuthorWithRole(user=UserReadAuthor.model_validate(user), authorship=resource_author.authorship, authorship_status=resource_author.authorship_status, creation_date=resource_author.creation_date, update_date=resource_author.update_date) for resource_author, user in author_results]
    course = CourseRead(**course.model_dump(), authors=authors)
    return course

async def get_course_meta(request: Request, course_uuid: str, with_unpublished_activities: bool, current_user: PublicUser | AnonymousUser, db_session: AsyncSession, slim: bool=False) -> FullCourseRead:
    from src.services.courses.chapters import get_course_chapters
    course_statement = select(Course, ResourceAuthor, User, Organization).outerjoin(ResourceAuthor, ResourceAuthor.resource_uuid == Course.course_uuid).outerjoin(User, ResourceAuthor.user_id == User.id).join(Organization, Organization.id == Course.org_id).where(Course.course_uuid == course_uuid).order_by(ResourceAuthor.id.asc())
    results = (await db_session.execute(course_statement)).all()
    if not results:
        raise HTTPException(status_code=404, detail='Course not found')
    course = results[0][0]
    org = results[0][3]
    author_results = [(ra, u) for _, ra, u, _ in results if ra is not None and u is not None]
    await check_resource_access(request, db_session, current_user, course.course_uuid, AccessAction.READ, context=AccessContext.DASHBOARD)
    is_anonymous = isinstance(current_user, AnonymousUser)
    if is_anonymous and course.published and (not with_unpublished_activities):
        from src.services.courses.cache import get_cached_course_meta
        cached = get_cached_course_meta(course_uuid, slim)
        if cached is not None:
            return FullCourseRead.model_validate(cached)
    chapters = []
    if course.id is not None:
        chapters = await get_course_chapters(request, course.id, db_session, current_user, with_unpublished_activities, slim=slim, course=course)
    authors = [AuthorWithRole(user=UserReadAuthor.model_validate(user), authorship=resource_author.authorship, authorship_status=resource_author.authorship_status, creation_date=resource_author.creation_date, update_date=resource_author.update_date) for resource_author, user in author_results]
    course_read = FullCourseRead(**course.model_dump(), org_uuid=org.org_uuid, authors=authors, chapters=chapters)
    if is_anonymous and course.published and (not with_unpublished_activities):
        from src.services.courses.cache import set_cached_course_meta
        set_cached_course_meta(course_uuid, slim, course_read.model_dump())
    return course_read

def _require_token_course_listing(current_user: APITokenUser, org_id: int) -> None:
    """API tokens list courses through their own ``courses.action_read`` and
    only in their own org, never through their creator's role."""
    if current_user.org_id != org_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='API token cannot access resources outside its organization')
    require_token_right(current_user, 'courses', 'action_read')

async def get_courses_orgslug(request: Request, current_user: PublicUser | AnonymousUser | APITokenUser, org_slug: str, db_session: AsyncSession, page: int=1, limit: int=10, include_unpublished: bool=False) -> List[CourseRead]:
    limit = min(limit, 100)
    acting_user_id = resolve_acting_user_id(current_user)
    is_anon = isinstance(current_user, AnonymousUser)
    if is_anon and (not include_unpublished):
        from src.services.courses.cache import get_cached_courses_list
        cached = get_cached_courses_list(org_slug, page, limit)
        if cached is not None:
            return [CourseRead.model_validate(c) for c in cached]
    offset = (page - 1) * limit
    org_statement = select(Organization).where(Organization.slug == org_slug)
    org = (await db_session.execute(org_statement)).scalars().first()
    if not org:
        return []
    is_token = isinstance(current_user, APITokenUser)
    if is_token:
        _require_token_course_listing(current_user, org.id)
    current_user = await viewer_for_org(db_session, current_user, org.id)
    can_view_unpublished = False
    if is_token:
        can_view_unpublished = include_unpublished
    elif include_unpublished and (not isinstance(current_user, AnonymousUser)):
        if await is_user_superadmin(acting_user_id, db_session):
            can_view_unpublished = True
        else:
            role_statement = select(Role).join(UserOrganization).where(UserOrganization.org_id == org.id).where(UserOrganization.user_id == acting_user_id)
            user_roles = (await db_session.execute(role_statement)).scalars().all()
            for role in user_roles:
                if role.id in ADMIN_OR_MAINTAINER_ROLE_IDS:
                    can_view_unpublished = True
                    break
    needs_distinct = False
    query = select(Course).join(Organization).where(Organization.slug == org_slug)
    if isinstance(current_user, AnonymousUser):
        query = query.where(Course.public == True, Course.published == True)
    elif is_token:
        if not can_view_unpublished:
            query = query.where(Course.published == True)
    elif can_view_unpublished:
        pass
    else:
        needs_distinct = True
        query = query.outerjoin(UserGroupResource, UserGroupResource.resource_uuid == Course.course_uuid).outerjoin(UserGroupUser, and_(UserGroupUser.usergroup_id == UserGroupResource.usergroup_id, UserGroupUser.user_id == acting_user_id)).outerjoin(ResourceAuthor, and_(ResourceAuthor.resource_uuid == Course.course_uuid, ResourceAuthor.user_id == acting_user_id, ResourceAuthor.authorship_status == ResourceAuthorshipStatusEnum.ACTIVE)).where(or_(and_(Course.published == True, Course.public == True), and_(Course.published == True, UserGroupResource.resource_uuid.is_(None), select(UserOrganization.id).where(UserOrganization.user_id == acting_user_id, UserOrganization.org_id == Course.org_id).exists()), UserGroupUser.user_id == acting_user_id, ResourceAuthor.user_id.isnot(None)))
    query = query.order_by(Course.creation_date.desc()).offset(offset).limit(limit)
    if needs_distinct:
        query = query.distinct()
    courses = (await db_session.execute(query)).scalars().all()
    if not courses:
        return []
    course_uuids = [course.course_uuid for course in courses]
    authors_query = select(ResourceAuthor, User).join(User, ResourceAuthor.user_id == User.id).where(ResourceAuthor.resource_uuid.in_(course_uuids)).order_by(ResourceAuthor.id.asc())
    author_results = (await db_session.execute(authors_query)).all()
    course_authors = {}
    for resource_author, user in author_results:
        if resource_author.resource_uuid not in course_authors:
            course_authors[resource_author.resource_uuid] = []
        course_authors[resource_author.resource_uuid].append(AuthorWithRole(user=UserReadAuthor.model_validate(user), authorship=resource_author.authorship, authorship_status=resource_author.authorship_status, creation_date=resource_author.creation_date, update_date=resource_author.update_date))
    course_reads = []
    for course in courses:
        course_read = CourseRead.model_validate({**course.model_dump(), 'id': course.id or 0, 'authors': course_authors.get(course.course_uuid, [])})
        course_reads.append(course_read)
    if is_anon and (not include_unpublished) and course_reads:
        from src.services.courses.cache import set_cached_courses_list
        set_cached_courses_list(org_slug, page, limit, [cr.model_dump() for cr in course_reads])
    return course_reads

async def get_courses_count_orgslug(request: Request, current_user: PublicUser | AnonymousUser | APITokenUser, org_slug: str, db_session: AsyncSession) -> int:
    """
    Get total count of courses for an organization (respecting visibility rules)
    """
    acting_user_id = resolve_acting_user_id(current_user)
    query = select(func.count(Course.id.distinct())).join(Organization).where(Organization.slug == org_slug)
    if isinstance(current_user, AnonymousUser):
        query = query.where(Course.public == True, Course.published == True)
    elif isinstance(current_user, APITokenUser):
        org_id = (await db_session.execute(select(Organization.id).where(Organization.slug == org_slug))).scalars().first()
        if org_id is None:
            return 0
        _require_token_course_listing(current_user, org_id)
        query = query.where(Course.published == True)
    elif await is_user_superadmin(acting_user_id, db_session):
        pass
    else:
        query = query.outerjoin(UserGroupResource, UserGroupResource.resource_uuid == Course.course_uuid).outerjoin(UserGroupUser, and_(UserGroupUser.usergroup_id == UserGroupResource.usergroup_id, UserGroupUser.user_id == acting_user_id)).outerjoin(ResourceAuthor, and_(ResourceAuthor.resource_uuid == Course.course_uuid, ResourceAuthor.user_id == acting_user_id, ResourceAuthor.authorship_status == ResourceAuthorshipStatusEnum.ACTIVE)).where(or_(and_(Course.published == True, Course.public == True), and_(Course.published == True, UserGroupResource.resource_uuid.is_(None), select(UserOrganization.id).where(UserOrganization.user_id == acting_user_id, UserOrganization.org_id == Course.org_id).exists()), UserGroupUser.user_id == acting_user_id, ResourceAuthor.user_id.isnot(None)))
    count = (await db_session.execute(query)).scalar_one()
    return count

def accessible_courses_filter(acting_user_id: int, *, is_anonymous: bool, is_api_token: bool, is_superadmin: bool):
    """SQL boolean selecting the courses ``acting_user_id`` may see in a listing.

    One definition shared by course search and everything that hangs off a
    course (lesson search), so a lesson can never surface from a course its
    reader could not open. Built from EXISTS subqueries rather than joins so
    it composes into any query without DISTINCT.

    - Anonymous: published and public courses only.
    - API tokens: every published course in their org (the org scope is the
      caller's job).
    - Superadmins: everything.
    - Members: published public courses; published courses not attached to
      any usergroup, when they belong to the course's org; published courses
      in a usergroup they are in; and any course (drafts included) they are an
      active author of.
    """
    if is_anonymous:
        return and_(Course.public == True, Course.published == True)
    if is_api_token:
        return Course.published == True
    if is_superadmin:
        return sa_true()
    in_any_usergroup = select(UserGroupResource.id).where(UserGroupResource.resource_uuid == Course.course_uuid).exists()
    in_users_usergroup = select(UserGroupUser.id).join(UserGroupResource, UserGroupResource.usergroup_id == UserGroupUser.usergroup_id).where(UserGroupResource.resource_uuid == Course.course_uuid, UserGroupUser.user_id == acting_user_id).exists()
    is_active_author = select(ResourceAuthor.id).where(ResourceAuthor.resource_uuid == Course.course_uuid, ResourceAuthor.user_id == acting_user_id, ResourceAuthor.authorship_status == ResourceAuthorshipStatusEnum.ACTIVE).exists()
    is_org_member = select(UserOrganization.id).where(UserOrganization.user_id == acting_user_id, UserOrganization.org_id == Course.org_id).exists()
    return or_(and_(Course.published == True, Course.public == True), and_(Course.published == True, ~in_any_usergroup, is_org_member), and_(Course.published == True, in_users_usergroup), is_active_author)

async def search_courses(request: Request, current_user: PublicUser | AnonymousUser | APITokenUser, org_slug: str, search_query: str, db_session: AsyncSession, page: int=1, limit: int=10) -> List[CourseRead]:
    """
    Search courses within an organization.

    Every word of the query has to match one of the searched fields (name,
    description, about, learnings, tags), in any order. Pattern matching goes
    through parameterized ``ilike``; there is no string interpolation of user
    input.
    """
    limit = min(limit, 100)
    offset = (page - 1) * limit
    patterns = build_like_patterns(search_query)
    searched_columns = [Course.name, Course.description, Course.about, Course.learnings, Course.tags]
    term_filters = [or_(*(column.ilike(pattern, escape=LIKE_ESCAPE_CHAR) for column in searched_columns)) for pattern in patterns]
    query = select(Course).join(Organization).where(Organization.slug == org_slug).where(*term_filters)
    if not isinstance(current_user, (AnonymousUser, APITokenUser)):
        search_org_id = (await db_session.execute(select(Organization.id).where(Organization.slug == org_slug))).scalars().first()
        if search_org_id is not None:
            current_user = await viewer_for_org(db_session, current_user, search_org_id)
    search_acting_user_id = resolve_acting_user_id(current_user)
    is_api_token = isinstance(current_user, APITokenUser)
    is_anonymous = isinstance(current_user, AnonymousUser)
    if is_api_token:
        org_id = (await db_session.execute(select(Organization.id).where(Organization.slug == org_slug))).scalars().first()
        if org_id is None:
            return []
        _require_token_course_listing(current_user, org_id)
    is_superadmin = not is_anonymous and (not is_api_token) and await is_user_superadmin(search_acting_user_id, db_session)
    query = query.where(accessible_courses_filter(search_acting_user_id, is_anonymous=is_anonymous, is_api_token=is_api_token, is_superadmin=is_superadmin))
    query = query.order_by(Course.creation_date.desc()).offset(offset).limit(limit)
    courses = (await db_session.execute(query)).scalars().all()
    if not courses:
        return []
    course_uuids = [course.course_uuid for course in courses]
    authors_query = select(ResourceAuthor, User).join(User, ResourceAuthor.user_id == User.id).where(ResourceAuthor.resource_uuid.in_(course_uuids)).order_by(ResourceAuthor.id.asc())
    author_results = (await db_session.execute(authors_query)).all()
    course_authors: dict[str, list[AuthorWithRole]] = {}
    for resource_author, user in author_results:
        course_authors.setdefault(resource_author.resource_uuid, []).append(AuthorWithRole(user=UserReadAuthor.model_validate(user), authorship=resource_author.authorship, authorship_status=resource_author.authorship_status, creation_date=resource_author.creation_date, update_date=resource_author.update_date))
    course_reads = []
    for course in courses:
        course_read = CourseRead.model_validate({**course.model_dump(), 'id': course.id or 0, 'authors': course_authors.get(course.course_uuid, [])})
        course_reads.append(course_read)
    return course_reads

async def create_course(request: Request, org_id: int, course_object: CourseCreate, current_user: PublicUser | AnonymousUser | APITokenUser, db_session: AsyncSession, thumbnail_file: UploadFile | None=None, thumbnail_type: ThumbnailType=ThumbnailType.IMAGE, creation_source: str='manual'):
    """
    Create a new course

    SECURITY NOTES:
    - Requires proper permissions to create courses in the organization
    - User becomes the CREATOR of the course automatically
    - For API tokens, the user who created the token becomes the CREATOR
    - Course creation is subject to organization limits and permissions
    """
    course = Course.model_validate(course_object)
    await check_resource_access(request, db_session, current_user, 'course_x', AccessAction.CREATE)
    await require_org_create_permission(current_user, org_id, db_session, 'courses')
    await check_limits_with_usage('courses', org_id, db_session)
    course.org_id = org_id
    org_statement = select(Organization).where(Organization.id == org_id)
    org = (await db_session.execute(org_statement)).scalars().first()
    course.course_uuid = str(f'course_{uuid4()}')
    course.creation_date = str(datetime.now())
    course.update_date = str(datetime.now())
    if thumbnail_file and thumbnail_file.filename:
        name_in_disk = await upload_thumbnail(thumbnail_file, org.org_uuid, course.course_uuid)
        if thumbnail_type == ThumbnailType.IMAGE:
            course.thumbnail_image = name_in_disk
            course.thumbnail_type = ThumbnailType.IMAGE
        elif thumbnail_type == ThumbnailType.VIDEO:
            course.thumbnail_video = name_in_disk
            course.thumbnail_type = ThumbnailType.VIDEO
    else:
        course.thumbnail_image = ''
        course.thumbnail_video = ''
        course.thumbnail_type = ThumbnailType.IMAGE
    if isinstance(current_user, APITokenUser):
        author_user_id = current_user.created_by_user_id
    else:
        author_user_id = current_user.id
    resource_author = ResourceAuthor(resource_uuid=course.course_uuid, user_id=author_user_id, authorship=ResourceAuthorshipEnum.CREATOR, authorship_status=ResourceAuthorshipStatusEnum.ACTIVE, creation_date=str(datetime.now()), update_date=str(datetime.now()))
    try:
        db_session.add(course)
        await db_session.flush()
        await db_session.refresh(course)
        db_session.add(resource_author)
        await db_session.commit()
        await db_session.refresh(resource_author)
    except Exception:
        await db_session.rollback()
        raise
    authors_statement = select(ResourceAuthor, User).join(User, ResourceAuthor.user_id == User.id).where(ResourceAuthor.resource_uuid == course.course_uuid).order_by(ResourceAuthor.id.asc())
    author_results = (await db_session.execute(authors_statement)).all()
    authors = [AuthorWithRole(user=UserReadAuthor.model_validate(user), authorship=resource_author.authorship, authorship_status=resource_author.authorship_status, creation_date=resource_author.creation_date, update_date=resource_author.update_date) for resource_author, user in author_results]
    await increase_feature_usage('courses', course.org_id, db_session)
    await track(event_name=analytics_events.COURSE_CREATED, org_id=course.org_id, user_id=author_user_id, properties={'course_uuid': course.course_uuid, 'source': creation_source})
    await dispatch_webhooks(event_name='course_created', org_id=course.org_id, data={'course_uuid': course.course_uuid, 'name': course.name, 'org_id': course.org_id})
    course_data = {key: getattr(course, key) for key in course.model_fields}
    return CourseRead.model_validate({**course_data, 'authors': authors})

async def update_course_thumbnail(request: Request, course_uuid: str, current_user: PublicUser | AnonymousUser, db_session: AsyncSession, thumbnail_file: UploadFile | None=None, thumbnail_type: ThumbnailType=ThumbnailType.IMAGE):
    statement = select(Course).where(Course.course_uuid == course_uuid)
    course = (await db_session.execute(statement)).scalars().first()
    name_in_disk = None
    if not course:
        raise HTTPException(status_code=404, detail='Course not found')
    await check_resource_access(request, db_session, current_user, course.course_uuid, AccessAction.UPDATE)
    org_statement = select(Organization).where(Organization.id == course.org_id)
    org = (await db_session.execute(org_statement)).scalars().first()
    if thumbnail_file and thumbnail_file.filename:
        name_in_disk = await upload_thumbnail(thumbnail_file, org.org_uuid, course.course_uuid)
    if name_in_disk:
        if thumbnail_type == ThumbnailType.IMAGE:
            course.thumbnail_image = name_in_disk
            course.thumbnail_type = ThumbnailType.IMAGE if not course.thumbnail_video else ThumbnailType.BOTH
        elif thumbnail_type == ThumbnailType.VIDEO:
            course.thumbnail_video = name_in_disk
            course.thumbnail_type = ThumbnailType.VIDEO if not course.thumbnail_image else ThumbnailType.BOTH
    else:
        raise HTTPException(status_code=500, detail='Issue with thumbnail upload')
    course.update_date = str(datetime.now())
    db_session.add(course)
    await db_session.commit()
    await db_session.refresh(course)
    authors_statement = select(ResourceAuthor, User).join(User, ResourceAuthor.user_id == User.id).where(ResourceAuthor.resource_uuid == course.course_uuid).order_by(ResourceAuthor.id.asc())
    author_results = (await db_session.execute(authors_statement)).all()
    authors = [AuthorWithRole(user=UserReadAuthor.model_validate(user), authorship=resource_author.authorship, authorship_status=resource_author.authorship_status, creation_date=resource_author.creation_date, update_date=resource_author.update_date) for resource_author, user in author_results]
    course = CourseRead(**course.model_dump(), authors=authors)
    return course

async def update_course(request: Request, course_object: CourseUpdate, course_uuid: str, current_user: PublicUser | AnonymousUser | APITokenUser, db_session: AsyncSession):
    """
    Update a course
    
    SECURITY NOTES:
    - Requires course ownership (CREATOR, MAINTAINER) or admin role
    - Sensitive fields (public, open_to_contributors) require additional validation
    - Cannot change course access settings without proper permissions
    """
    statement = select(Course).where(Course.course_uuid == course_uuid)
    course = (await db_session.execute(statement)).scalars().first()
    if not course:
        raise HTTPException(status_code=404, detail='Course not found')
    await check_resource_access(request, db_session, current_user, course.course_uuid, AccessAction.UPDATE)
    sensitive_fields_updated = []
    if course_object.public is not None:
        sensitive_fields_updated.append('public')
    if course_object.open_to_contributors is not None:
        sensitive_fields_updated.append('open_to_contributors')
    if sensitive_fields_updated:
        acting_user_id = resolve_acting_user_id(current_user)
        statement = select(ResourceAuthor).where(ResourceAuthor.resource_uuid == course_uuid, ResourceAuthor.user_id == acting_user_id)
        resource_author = (await db_session.execute(statement)).scalars().first()
        is_course_owner = False
        if resource_author:
            if (resource_author.authorship == ResourceAuthorshipEnum.CREATOR or resource_author.authorship == ResourceAuthorshipEnum.MAINTAINER) and resource_author.authorship_status == ResourceAuthorshipStatusEnum.ACTIVE:
                is_course_owner = True
        is_admin_or_maintainer = await authorization_verify_based_on_org_admin_status(request, acting_user_id, 'update', course_uuid, db_session)
        if not (is_course_owner or is_admin_or_maintainer):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"You must be the course owner (CREATOR or MAINTAINER) or have admin role to change access settings: {', '.join(sensitive_fields_updated)}")
    for field in ('thumbnail_image', 'thumbnail_video'):
        value = getattr(course_object, field)
        if value and value != getattr(course, field) and (not _is_bare_filename(value)):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f'Invalid {field}')
    old_published = course.published
    for var, value in vars(course_object).items():
        if value is not None:
            setattr(course, var, value)
    course.update_date = str(datetime.now())
    db_session.add(course)
    await db_session.commit()
    await db_session.refresh(course)
    notify_ee_course_content_changed(course.id)
    if course_object.published is not None and course.published != old_published:
        await track(event_name=analytics_events.COURSE_PUBLISHED, org_id=course.org_id, user_id=resolve_acting_user_id(current_user), properties={'course_uuid': course.course_uuid, 'published': bool(course.published)})
        await dispatch_webhooks(event_name='course_published', org_id=course.org_id, data={'course_uuid': course.course_uuid, 'name': course.name, 'published': course.published})
    authors_statement = select(ResourceAuthor, User).join(User, ResourceAuthor.user_id == User.id).where(ResourceAuthor.resource_uuid == course.course_uuid).order_by(ResourceAuthor.id.asc())
    author_results = (await db_session.execute(authors_statement)).all()
    authors = [AuthorWithRole(user=UserReadAuthor.model_validate(user), authorship=resource_author.authorship, authorship_status=resource_author.authorship_status, creation_date=resource_author.creation_date, update_date=resource_author.update_date) for resource_author, user in author_results]
    course = CourseRead(**course.model_dump(), authors=authors)
    return course

def _strip_course_from_landing(landing: dict, course_uuid: str) -> bool:
    """Drop course_uuid from every featured-courses section. Returns True if anything changed."""
    changed = False
    sections = landing.get('sections') if isinstance(landing, dict) else None
    if not isinstance(sections, list):
        return False
    for section in sections:
        if not isinstance(section, dict) or section.get('type') != 'featured-courses':
            continue
        courses = section.get('courses')
        if isinstance(courses, list) and course_uuid in courses:
            section['courses'] = [c for c in courses if c != course_uuid]
            changed = True
    return changed

async def _remove_course_from_org_landing(org_id: int, org_slug: str | None, course_uuid: str, db_session: AsyncSession) -> None:
    """
    Landing pages reference featured courses by uuid inside the org config JSON,
    so nothing cascades when a course is deleted. Left alone, the dead uuid stays
    in the landing forever and the section renders short (or empty) for visitors.
    """
    statement = select(OrganizationConfig).where(OrganizationConfig.org_id == org_id)
    org_config = (await db_session.execute(statement)).scalars().first()
    if org_config is None or not org_config.config:
        return
    updated_config = json.loads(json.dumps(org_config.config))
    is_v2 = str(updated_config.get('config_version', '1.0')).startswith('2')
    landing = (updated_config.get('customization') or {}).get('landing') if is_v2 else updated_config.get('landing')
    if not landing or not _strip_course_from_landing(landing, course_uuid):
        return
    org_config.config = updated_config
    org_config.update_date = str(datetime.now())
    db_session.add(org_config)
    await db_session.commit()
    from src.services.orgs.cache import invalidate_org_cache, invalidate_org_config_cache
    invalidate_org_config_cache(org_id)
    if org_slug:
        invalidate_org_cache(org_slug)

async def delete_course(request: Request, course_uuid: str, current_user: PublicUser | AnonymousUser, db_session: AsyncSession):
    statement = select(Course).where(Course.course_uuid == course_uuid)
    course = (await db_session.execute(statement)).scalars().first()
    if not course:
        raise HTTPException(status_code=404, detail='Course not found')
    await check_resource_access(request, db_session, current_user, course.course_uuid, AccessAction.DELETE)
    org_statement = select(Organization).where(Organization.id == course.org_id)
    org = (await db_session.execute(org_statement)).scalars().first()
    if org:
        from src.services.courses.transfer.storage_utils import delete_storage_directory
        content_path = f'content/orgs/{org.org_uuid}/courses/{course_uuid}'
        delete_storage_directory(content_path)
    course_uuid_val = course.course_uuid
    course_name_val = course.name
    course_org_id = course.org_id
    org_slug_val = org.slug if org else None
    await db_session.delete(course)
    await db_session.commit()
    try:
        await _remove_course_from_org_landing(course_org_id, org_slug_val, course_uuid_val, db_session)
    except Exception:
        logger.exception('Failed to remove deleted course %s from org landing', course_uuid_val)
        await db_session.rollback()
    await decrease_feature_usage('courses', course_org_id, db_session)
    await dispatch_webhooks(event_name='course_deleted', org_id=course_org_id, data={'course_uuid': course_uuid_val, 'name': course_name_val})
    return {'detail': 'Course deleted'}

async def get_user_courses(request: Request, current_user: PublicUser | AnonymousUser, user_id: int, db_session: AsyncSession, page: int=1, limit: int=10) -> List[CourseRead]:
    await authorization_verify_if_user_is_anon(current_user.id)
    can_see_unpublished = int(current_user.id) == int(user_id) or await is_user_superadmin(int(current_user.id), db_session)
    statement = select(Course).join(ResourceAuthor, ResourceAuthor.resource_uuid == Course.course_uuid).where(ResourceAuthor.user_id == user_id, ResourceAuthor.authorship_status == ResourceAuthorshipStatusEnum.ACTIVE)
    if not can_see_unpublished:
        statement = statement.where(Course.published == True, Course.public == True)
    statement = statement.offset((page - 1) * limit).limit(limit)
    courses = (await db_session.execute(statement)).scalars().all()
    if not courses:
        return []
    course_uuids = [course.course_uuid for course in courses]
    authors_query = select(ResourceAuthor, User).join(User, ResourceAuthor.user_id == User.id).where(ResourceAuthor.resource_uuid.in_(course_uuids)).order_by(ResourceAuthor.id.asc())
    author_results = (await db_session.execute(authors_query)).all()
    course_authors: dict[str, list[AuthorWithRole]] = {}
    for resource_author, user in author_results:
        course_authors.setdefault(resource_author.resource_uuid, []).append(AuthorWithRole(user=UserReadAuthor.model_validate(user), authorship=resource_author.authorship, authorship_status=resource_author.authorship_status, creation_date=resource_author.creation_date, update_date=resource_author.update_date))
    result = []
    for course in courses:
        course_read = CourseRead.model_validate({**course.model_dump(), 'id': course.id or 0, 'authors': course_authors.get(course.course_uuid, [])})
        result.append(course_read)
    return result

def _copy_storage_file(src_path: str, dst_path: str) -> None:
    """Copy a file using S3 or local filesystem."""
    import os
    import shutil
    from src.services.courses.transfer.storage_utils import is_s3_enabled, read_file_content, upload_to_s3
    if is_s3_enabled():
        content = read_file_content(src_path)
        if content:
            upload_to_s3(dst_path, content)
    else:
        os.makedirs(os.path.dirname(dst_path), exist_ok=True)
        shutil.copy2(src_path, dst_path)

def _delete_storage_file(file_path: str) -> None:
    """Delete a file from S3 or local filesystem."""
    from src.services.courses.transfer.storage_utils import delete_storage_file
    delete_storage_file(file_path)

def _replace_uuids_in_content(content, uuid_map):
    """Recursively replace UUID values in a nested dict/list structure."""
    if isinstance(content, dict):
        return {k: _replace_uuids_in_content(v, uuid_map) for k, v in content.items()}
    elif isinstance(content, list):
        return [_replace_uuids_in_content(item, uuid_map) for item in content]
    elif isinstance(content, str) and content in uuid_map:
        return uuid_map[content]
    return content

def _delete_storage_directory(dir_path: str) -> None:
    """Delete a directory from S3 or local filesystem."""
    from src.services.courses.transfer.storage_utils import delete_storage_directory
    delete_storage_directory(dir_path)

def _copy_storage_directory(src_dir: str, dst_dir: str) -> None:
    """Recursively copy a directory using S3 or local filesystem."""
    import os
    import shutil
    from src.services.courses.transfer.storage_utils import is_s3_enabled, get_storage_client, get_s3_bucket_name
    if is_s3_enabled():
        s3_client = get_storage_client()
        if not s3_client:
            return
        bucket = get_s3_bucket_name()
        prefix = src_dir if src_dir.endswith('/') else src_dir + '/'
        paginator = s3_client.get_paginator('list_objects_v2')
        try:
            for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                for obj in page.get('Contents', []):
                    old_key = obj['Key']
                    new_key = dst_dir + old_key[len(src_dir):]
                    s3_client.copy_object(Bucket=bucket, CopySource={'Bucket': bucket, 'Key': old_key}, Key=new_key)
        except Exception as e:
            logger.error('Error copying S3 directory %s -> %s: %s', src_dir, dst_dir, e, exc_info=True)
    elif os.path.exists(src_dir):
        shutil.copytree(src_dir, dst_dir, dirs_exist_ok=True)

async def clone_activity(db_session: AsyncSession, original_activity, original_blocks: list, *, org_uuid: str, org_id: int, source_course_path: str, new_course_uuid: str, new_course_path: str, new_course_id: int, new_chapter_id: int, order: int, published: bool | None=None):
    """Copy one activity (row, chapter link, files and blocks) into a chapter.

    Used by clone_course and by the agent's copy tool. The activity's files are
    copied under the new activity, and every block uuid, file id and activity
    uuid its content names is pointed at the copies. ``published`` defaults to
    the original's state. Adds to the session without committing.
    """
    from src.services.courses.transfer.storage_utils import list_directory, rewrite_sqlite_db_paths
    from src.db.courses.activities import Activity
    from src.db.courses.blocks import Block
    from src.db.courses.chapter_activities import ChapterActivity
    new_activity_uuid = f'activity_{uuid4()}'
    new_content = dict(original_activity.content) if original_activity.content else {}
    new_details = dict(original_activity.details) if original_activity.details else {}
    new_content = rewrite_sqlite_db_paths(new_content, original_activity.activity_uuid, f'orgs/{org_uuid}/courses/{new_course_uuid}/activities/{new_activity_uuid}')
    new_activity = Activity(name=original_activity.name, activity_type=original_activity.activity_type, activity_sub_type=original_activity.activity_sub_type, content=new_content, details=new_details, published=original_activity.published if published is None else published, org_id=org_id, course_id=new_course_id, activity_uuid=new_activity_uuid, creation_date=str(datetime.now()), update_date=str(datetime.now()), extra_metadata=original_activity.extra_metadata)
    db_session.add(new_activity)
    await db_session.flush()
    new_chapter_activity = ChapterActivity(chapter_id=new_chapter_id, activity_id=new_activity.id, course_id=new_course_id, org_id=org_id, order=order, creation_date=str(datetime.now()), update_date=str(datetime.now()))
    db_session.add(new_chapter_activity)
    original_activity_path = f'{source_course_path}/activities/{original_activity.activity_uuid}'
    new_activity_path = f'{new_course_path}/activities/{new_activity_uuid}'
    _copy_storage_directory(original_activity_path, new_activity_path)
    content_ref_map = {original_activity.activity_uuid: new_activity_uuid}
    if original_activity.activity_type.value == 'TYPE_DYNAMIC':
        for original_block in original_blocks:
            new_block_uuid = f'block_{uuid4()}'
            content_ref_map[original_block.block_uuid] = new_block_uuid
            new_block_content = dict(original_block.content) if original_block.content else {}
            if 'activity_uuid' in new_block_content:
                new_block_content['activity_uuid'] = new_activity_uuid
            block_type_folder = ''
            if original_block.block_type.value == 'BLOCK_VIDEO':
                block_type_folder = 'videoBlock'
            elif original_block.block_type.value == 'BLOCK_IMAGE':
                block_type_folder = 'imageBlock'
            elif original_block.block_type.value == 'BLOCK_DOCUMENT_PDF':
                block_type_folder = 'pdfBlock'
            elif original_block.block_type.value == 'BLOCK_AUDIO':
                block_type_folder = 'audioBlock'
            if block_type_folder:
                old_copied_block_path = f'{new_activity_path}/dynamic/blocks/{block_type_folder}/{original_block.block_uuid}'
                new_block_path_str = f'{new_activity_path}/dynamic/blocks/{block_type_folder}/{new_block_uuid}'
                if block_type_folder == 'videoBlock':
                    _copy_storage_directory(f'{old_copied_block_path}/hls', f'{new_block_path_str}/hls')
                block_files = list_directory(old_copied_block_path)
                original_file_id = new_block_content.get('file_id')
                file_id_matched = False
                if block_files:
                    for filename in block_files:
                        old_file_key = f'{old_copied_block_path}/{filename}'
                        new_file_id = str(uuid4())
                        file_stem, file_ext = filename.rsplit('.', 1) if '.' in filename else (filename, '')
                        new_filename = f'block_{new_file_id}.{file_ext}' if file_ext else f'block_{new_file_id}'
                        new_file_key = f'{new_block_path_str}/{new_filename}'
                        _copy_storage_file(old_file_key, new_file_key)
                        _delete_storage_file(old_file_key)
                        if file_stem == original_file_id:
                            new_block_content['file_id'] = f'block_{new_file_id}'
                            file_id_matched = True
                        elif 'file_id' in new_block_content and (not file_id_matched):
                            new_block_content['file_id'] = f'block_{new_file_id}'
                        if file_stem:
                            content_ref_map[file_stem] = f'block_{new_file_id}'
                _delete_storage_directory(old_copied_block_path)
            new_block = Block(block_type=original_block.block_type, content=new_block_content, org_id=org_id, course_id=new_course_id, chapter_id=new_chapter_id, activity_id=new_activity.id, block_uuid=new_block_uuid, creation_date=str(datetime.now()), update_date=str(datetime.now()))
            db_session.add(new_block)
    if new_content:
        new_activity.content = _replace_uuids_in_content(new_content, content_ref_map)
        db_session.add(new_activity)
    return new_activity

async def clone_course(request: Request, course_uuid: str, current_user: PublicUser | AnonymousUser | APITokenUser, db_session: AsyncSession) -> CourseRead:
    """
    Clone a course with all its chapters, activities, blocks, and files.

    This creates a complete copy of the course including:
    - Course metadata (name, description, about, learnings, tags, SEO, etc.)
    - Thumbnail files
    - Chapters with their ordering
    - Activities with their files (videos, documents, PDFs)
    - Dynamic activity blocks with their files (images, videos, PDFs)

    The cloned course will:
    - Have a new course_uuid
    - Have "(Copy)" appended to the name
    - Be set to private (public=False) by default
    - Have the current user as the creator
    """
    import os
    from src.services.courses.transfer.storage_utils import is_s3_enabled, file_exists
    from collections import defaultdict
    from src.db.courses.chapters import Chapter
    from src.db.courses.activities import Activity
    from src.db.courses.blocks import Block
    from src.db.courses.course_chapters import CourseChapter
    from src.db.courses.chapter_activities import ChapterActivity
    statement = select(Course).where(Course.course_uuid == course_uuid)
    original_course = (await db_session.execute(statement)).scalars().first()
    if not original_course:
        raise HTTPException(status_code=404, detail='Course not found')
    await check_resource_access(request, db_session, current_user, original_course.course_uuid, AccessAction.UPDATE)
    await check_resource_access(request, db_session, current_user, 'course_x', AccessAction.CREATE)
    await require_org_create_permission(current_user, original_course.org_id, db_session, 'courses')
    await check_limits_with_usage('courses', original_course.org_id, db_session)
    org_statement = select(Organization).where(Organization.id == original_course.org_id)
    org = (await db_session.execute(org_statement)).scalars().first()
    if not org:
        raise HTTPException(status_code=404, detail='Organization not found')
    new_course_uuid = str(f'course_{uuid4()}')
    new_course = Course(org_id=original_course.org_id, name=f'{original_course.name} (Copy)', description=original_course.description, about=original_course.about, learnings=original_course.learnings, tags=original_course.tags, thumbnail_type=original_course.thumbnail_type, thumbnail_image='', thumbnail_video='', public=False, published=False, open_to_contributors=False, course_uuid=new_course_uuid, creation_date=str(datetime.now()), update_date=str(datetime.now()), seo=original_course.seo, extra_metadata=original_course.extra_metadata)
    content_base = 'content/orgs'
    original_course_path = f'{content_base}/{org.org_uuid}/courses/{original_course.course_uuid}'
    new_course_path = f'{content_base}/{org.org_uuid}/courses/{new_course_uuid}'
    if not is_s3_enabled():
        os.makedirs(f'{new_course_path}/thumbnails', exist_ok=True)
    if original_course.thumbnail_image and _is_bare_filename(original_course.thumbnail_image):
        original_thumbnail_path = f'{original_course_path}/thumbnails/{original_course.thumbnail_image}'
        if file_exists(original_thumbnail_path):
            new_thumbnail_name = f"{new_course_uuid}_thumbnail_{uuid4()}.{original_course.thumbnail_image.split('.')[-1]}"
            new_thumbnail_path = f'{new_course_path}/thumbnails/{new_thumbnail_name}'
            _copy_storage_file(original_thumbnail_path, new_thumbnail_path)
            new_course.thumbnail_image = new_thumbnail_name
    if original_course.thumbnail_video and _is_bare_filename(original_course.thumbnail_video):
        original_video_path = f'{original_course_path}/thumbnails/{original_course.thumbnail_video}'
        if file_exists(original_video_path):
            new_video_name = f"{new_course_uuid}_thumbnail_{uuid4()}.{original_course.thumbnail_video.split('.')[-1]}"
            new_video_path = f'{new_course_path}/thumbnails/{new_video_name}'
            _copy_storage_file(original_video_path, new_video_path)
            new_course.thumbnail_video = new_video_name
    db_session.add(new_course)
    await db_session.flush()
    await db_session.refresh(new_course)
    if isinstance(current_user, APITokenUser):
        author_user_id = current_user.created_by_user_id
    else:
        author_user_id = current_user.id
    resource_author = ResourceAuthor(resource_uuid=new_course.course_uuid, user_id=author_user_id, authorship=ResourceAuthorshipEnum.CREATOR, authorship_status=ResourceAuthorshipStatusEnum.ACTIVE, creation_date=str(datetime.now()), update_date=str(datetime.now()))
    db_session.add(resource_author)
    await db_session.flush()
    statement = select(Chapter, CourseChapter).join(CourseChapter, Chapter.id == CourseChapter.chapter_id).where(CourseChapter.course_id == original_course.id).order_by(CourseChapter.order)
    chapter_results = (await db_session.execute(statement)).all()
    _original_chapter_ids = [ch.id for ch, _ in chapter_results]
    _all_act_results = (await db_session.execute(select(Activity, ChapterActivity).join(ChapterActivity, Activity.id == ChapterActivity.activity_id).where(ChapterActivity.chapter_id.in_(_original_chapter_ids)).order_by(ChapterActivity.order))).all()
    _activities_by_chapter: dict = defaultdict(list)
    for _act, _ca in _all_act_results:
        _activities_by_chapter[_ca.chapter_id].append((_act, _ca))
    _dynamic_activity_ids = [_act.id for _act, _ in _all_act_results if _act.activity_type.value == 'TYPE_DYNAMIC']
    if _dynamic_activity_ids:
        _all_blocks = (await db_session.execute(select(Block).where(Block.activity_id.in_(_dynamic_activity_ids)))).scalars().all()
        _blocks_by_activity: dict = defaultdict(list)
        for _blk in _all_blocks:
            _blocks_by_activity[_blk.activity_id].append(_blk)
    else:
        _blocks_by_activity = {}
    chapter_id_map = {}
    for original_chapter, course_chapter in chapter_results:
        new_chapter_uuid = f'chapter_{uuid4()}'
        new_chapter = Chapter(name=original_chapter.name, description=original_chapter.description, thumbnail_image=original_chapter.thumbnail_image, chapter_uuid=new_chapter_uuid, org_id=original_course.org_id, course_id=new_course.id, creation_date=str(datetime.now()), update_date=str(datetime.now()), extra_metadata=original_chapter.extra_metadata)
        db_session.add(new_chapter)
        await db_session.flush()
        chapter_id_map[original_chapter.id] = new_chapter.id
        new_course_chapter = CourseChapter(course_id=new_course.id, chapter_id=new_chapter.id, org_id=original_course.org_id, order=course_chapter.order, creation_date=str(datetime.now()), update_date=str(datetime.now()))
        db_session.add(new_course_chapter)
        activity_results = _activities_by_chapter.get(original_chapter.id, [])
        for original_activity, chapter_activity in activity_results:
            await clone_activity(db_session, original_activity, _blocks_by_activity.get(original_activity.id, []), org_uuid=org.org_uuid, org_id=original_course.org_id, source_course_path=original_course_path, new_course_uuid=new_course_uuid, new_course_path=new_course_path, new_course_id=new_course.id, new_chapter_id=new_chapter.id, order=chapter_activity.order)
    await db_session.commit()
    await increase_feature_usage('courses', new_course.org_id, db_session)
    await track(event_name=analytics_events.COURSE_CREATED, org_id=new_course.org_id, user_id=author_user_id, properties={'course_uuid': new_course.course_uuid, 'source': 'clone'})
    authors_statement = select(ResourceAuthor, User).join(User, ResourceAuthor.user_id == User.id).where(ResourceAuthor.resource_uuid == new_course.course_uuid).order_by(ResourceAuthor.id.asc())
    author_results = (await db_session.execute(authors_statement)).all()
    authors = [AuthorWithRole(user=UserReadAuthor.model_validate(user), authorship=resource_author.authorship, authorship_status=resource_author.authorship_status, creation_date=resource_author.creation_date, update_date=resource_author.update_date) for resource_author, user in author_results]
    course_read = CourseRead(**new_course.model_dump(), authors=authors)
    return CourseRead.model_validate(course_read)

async def get_course_user_rights(request: Request, course_uuid: str, current_user: PublicUser | AnonymousUser | APITokenUser, db_session: AsyncSession) -> dict:
    """
    Get detailed user rights for a specific course.
    
    This function returns comprehensive rights information that can be used
    by the UI to enable/disable features based on user permissions.
    
    SECURITY NOTES:
    - Returns rights based on course ownership and user roles
    - Includes both course-level and content-level permissions
    - Safe to expose to UI as it only returns permission information
    """
    statement = select(Course).where(Course.course_uuid == course_uuid)
    course = (await db_session.execute(statement)).scalars().first()
    if not course:
        raise HTTPException(status_code=404, detail='Course not found')
    rights_acting_user_id = resolve_acting_user_id(current_user)
    rights = {'course_uuid': course_uuid, 'user_id': rights_acting_user_id, 'is_anonymous': rights_acting_user_id == 0, 'permissions': {'read': False, 'create': False, 'update': False, 'delete': False, 'create_content': False, 'update_content': False, 'delete_content': False, 'manage_contributors': False, 'manage_access': False, 'grade_assignments': False, 'mark_activities_done': False, 'create_certifications': False}, 'ownership': {'is_owner': False, 'is_creator': False, 'is_maintainer': False, 'is_contributor': False, 'authorship_status': None}, 'roles': {'is_admin': False, 'is_maintainer_role': False, 'is_instructor': False, 'is_user': False}}
    if rights_acting_user_id == 0:
        if course.public:
            rights['permissions']['read'] = True
        return rights
    statement = select(ResourceAuthor).where(ResourceAuthor.resource_uuid == course_uuid, ResourceAuthor.user_id == rights_acting_user_id)
    resource_author = (await db_session.execute(statement)).scalars().first()
    if resource_author:
        rights['ownership']['authorship_status'] = resource_author.authorship_status
        if resource_author.authorship_status == ResourceAuthorshipStatusEnum.ACTIVE:
            if resource_author.authorship == ResourceAuthorshipEnum.CREATOR:
                rights['ownership']['is_creator'] = True
                rights['ownership']['is_owner'] = True
            elif resource_author.authorship == ResourceAuthorshipEnum.MAINTAINER:
                rights['ownership']['is_maintainer'] = True
                rights['ownership']['is_owner'] = True
            elif resource_author.authorship == ResourceAuthorshipEnum.CONTRIBUTOR:
                rights['ownership']['is_contributor'] = True
                rights['ownership']['is_owner'] = True
    from src.security.rbac.rbac import authorization_verify_based_on_org_admin_status
    from src.security.rbac.rbac import authorization_verify_based_on_roles
    is_admin_or_maintainer = await authorization_verify_based_on_org_admin_status(request, rights_acting_user_id, 'update', course_uuid, db_session)
    if is_admin_or_maintainer:
        rights['roles']['is_admin'] = True
        rights['roles']['is_maintainer_role'] = True
    has_instructor_permissions = await authorization_verify_based_on_roles(request, rights_acting_user_id, 'create', 'course_x', db_session)
    if has_instructor_permissions:
        rights['roles']['is_instructor'] = True
    has_user_permissions = await authorization_verify_based_on_roles(request, rights_acting_user_id, 'read', course_uuid, db_session)
    if has_user_permissions:
        rights['roles']['is_user'] = True
    is_course_owner = rights['ownership']['is_owner']
    is_admin = rights['roles']['is_admin']
    is_maintainer_role = rights['roles']['is_maintainer_role']
    is_instructor = rights['roles']['is_instructor']
    if course.public or is_course_owner or is_admin or is_maintainer_role or is_instructor or has_user_permissions:
        rights['permissions']['read'] = True
    if is_instructor or is_admin or is_maintainer_role:
        rights['permissions']['create'] = True
    if is_course_owner or is_admin or is_maintainer_role:
        rights['permissions']['update'] = True
    if is_course_owner or is_admin or is_maintainer_role:
        rights['permissions']['delete'] = True
    if is_course_owner or is_admin or is_maintainer_role:
        rights['permissions']['create_content'] = True
    if is_course_owner or is_admin or is_maintainer_role:
        rights['permissions']['update_content'] = True
    if is_course_owner or is_admin or is_maintainer_role:
        rights['permissions']['delete_content'] = True
    if is_course_owner or is_admin or is_maintainer_role:
        rights['permissions']['manage_contributors'] = True
    if rights['ownership']['is_creator'] or rights['ownership']['is_maintainer'] or is_admin or is_maintainer_role:
        rights['permissions']['manage_access'] = True
    if is_course_owner or is_admin or is_maintainer_role:
        rights['permissions']['grade_assignments'] = True
    if is_course_owner or is_admin or is_maintainer_role:
        rights['permissions']['mark_activities_done'] = True
    if is_course_owner or is_admin or is_maintainer_role:
        rights['permissions']['create_certifications'] = True
    return rights
