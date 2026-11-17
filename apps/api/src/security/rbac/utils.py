from typing import Optional
from fastapi import HTTPException, status
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

async def check_element_type(element_uuid):
    """
    Check the element type (course, user, house, folder, media, …) by its UUID prefix
    """
    if element_uuid.startswith('course_'):
        return 'courses'
    elif element_uuid.startswith('user_'):
        return 'users'
    elif element_uuid.startswith('usergroup_'):
        return 'usergroups'
    elif element_uuid.startswith('house_'):
        return 'houses'
    elif element_uuid.startswith('org_'):
        return 'organizations'
    elif element_uuid.startswith('chapter_'):
        return 'coursechapters'
    elif element_uuid.startswith('activity_'):
        return 'activities'
    elif element_uuid.startswith('role_'):
        return 'roles'
    elif element_uuid.startswith('community_'):
        return 'communities'
    elif element_uuid.startswith('discussion_'):
        return 'discussions'
    elif element_uuid.startswith('vote_'):
        return 'votes'
    elif element_uuid.startswith('livesession_'):
        return 'live_sessions'
    else:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='User rights : Issue verifying element nature')

async def check_course_permissions_with_own(element_rights, action: str, is_author: bool=False) -> bool:
    """
    Check course-specific permissions including "own" permissions.

    Args:
        element_rights: The rights object for courses (PermissionsWithOwn) or dict from JSON
        action: The action to check ("read", "update", "delete", "create")
        is_author: Whether the user is the author of the course

    Returns:
        bool: True if permission is granted, False otherwise
    """
    if not element_rights:
        return False

    def get_permission(key: str) -> bool:
        if isinstance(element_rights, dict):
            return element_rights.get(key, False)
        return getattr(element_rights, key, False)
    if get_permission(f'action_{action}'):
        return True
    if is_author:
        own_action = f'action_{action}_own'
        if get_permission(own_action):
            return True
    return False

async def get_singular_form_of_element(element_uuid):
    element_type = await check_element_type(element_uuid)
    if element_type == 'activities':
        return 'activity'
    else:
        singular_form_element = element_type[:-1]
        return singular_form_element

async def get_id_identifier_of_element(element_uuid):
    singular_form_element = await get_singular_form_of_element(element_uuid)
    if singular_form_element == 'organization':
        return 'org_id'
    else:
        return str(singular_form_element) + '_id'

async def get_element_organization_id(element_uuid: str, db_session: AsyncSession) -> Optional[int]:
    """Get the organization ID that an element belongs to (see
    ``_lookup_element_organization_id``).

    A request's admin, role and token checks all ask this about the same
    element, so the answer is memoized per session. Only found orgs are
    remembered: an element created later in the session must still resolve.
    """
    element_type = await check_element_type(element_uuid)
    if element_uuid.endswith('_x'):
        return None
    cache = getattr(db_session, '_element_org_cache', None)
    if not isinstance(cache, dict):
        cache = {}
        try:
            db_session._element_org_cache = cache
        except Exception:
            pass
    from src.security.rbac.resource_access import _loaded_in_session, _resource_model
    located = _resource_model(element_type)
    if located is not None:
        model, uuid_field = located
        row = _loaded_in_session(db_session, model, uuid_field, element_uuid)
        if row is not None and getattr(row, 'org_id', None) is not None:
            return row.org_id
    if element_uuid in cache:
        return cache[element_uuid]
    org_id = await _lookup_element_organization_id(element_uuid, db_session)
    if org_id is not None:
        cache[element_uuid] = org_id
    return org_id

async def _lookup_element_organization_id(element_uuid: str, db_session: AsyncSession) -> Optional[int]:
    """
    Get the organization ID that an element belongs to.

    This is CRITICAL for API token authorization - tokens can only access
    resources within their organization.

    Args:
        element_uuid: The UUID of the element
        db_session: Database session

    Returns:
        Optional[int]: The organization ID, or None if not applicable
    """
    from src.db.courses.courses import Course
    from src.db.courses.chapters import Chapter
    from src.db.courses.activities import Activity
    from src.db.organizations import Organization
    from src.db.roles import Role
    from src.db.usergroups import UserGroup
    element_type = await check_element_type(element_uuid)
    if element_type == 'courses':
        return (await db_session.execute(select(Course.org_id).where(Course.course_uuid == element_uuid))).scalars().first()
    elif element_type == 'coursechapters':
        return (await db_session.execute(select(Chapter.org_id).where(Chapter.chapter_uuid == element_uuid))).scalars().first()
    elif element_type == 'activities':
        return (await db_session.execute(select(Activity.org_id).where(Activity.activity_uuid == element_uuid))).scalars().first()
    elif element_type == 'organizations':
        return (await db_session.execute(select(Organization.id).where(Organization.org_uuid == element_uuid))).scalars().first()
    elif element_type == 'roles':
        return (await db_session.execute(select(Role.org_id).where(Role.role_uuid == element_uuid))).scalars().first()
    elif element_type == 'usergroups':
        return (await db_session.execute(select(UserGroup.org_id).where(UserGroup.usergroup_uuid == element_uuid))).scalars().first()
    elif element_type == 'users':
        return None
    elif element_type == 'houses':
        return None
    elif element_type == 'communities':
        from src.db.communities.communities import Community
        return (await db_session.execute(select(Community.org_id).where(Community.community_uuid == element_uuid))).scalars().first()
    elif element_type == 'discussions':
        from src.db.communities.discussions import Discussion
        return (await db_session.execute(select(Discussion.org_id).where(Discussion.discussion_uuid == element_uuid))).scalars().first()
    elif element_type == 'votes':
        from src.db.communities.discussion_votes import DiscussionVote
        from src.db.communities.discussions import Discussion
        return (await db_session.execute(select(Discussion.org_id).join(DiscussionVote, Discussion.id == DiscussionVote.discussion_id).where(DiscussionVote.vote_uuid == element_uuid))).scalars().first()
    return None
