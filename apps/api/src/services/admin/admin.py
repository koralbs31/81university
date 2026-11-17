"""
Admin API service layer.

Provides headless API operations using API token authentication.
All functions require an APITokenUser and operate within the token's org scope.
"""
from datetime import datetime, timedelta
from typing import List, Optional
from urllib.parse import quote
from uuid import uuid4
from fastapi import HTTPException, Request, status
from sqlmodel import select, func
from sqlmodel.ext.asyncio.session import AsyncSession
from src.db.courses.activities import Activity
from src.db.courses.chapter_activities import ChapterActivity
from src.db.courses.chapters import Chapter
from src.db.courses.course_chapters import CourseChapter
from src.db.courses.courses import Course
from src.db.courses.certifications import CertificateUser, CertificateUserRead, CertificationRead, Certifications
from src.db.organizations import Organization
from src.db.trail_runs import TrailRun
from src.db.trail_steps import TrailStep
from src.db.trails import Trail, TrailRead
from src.db.api_tokens import APIToken
from src.db.roles import Role
from src.db.usergroup_resources import UserGroupResource
from src.db.usergroup_user import UserGroupUser
from src.db.usergroups import UserGroup, UserGroupRead
from src.db.user_organizations import UserOrganization
from src.db.users import APITokenUser, OrgMemberUserRead, SuperadminAPITokenUser, User, UserRead
from src.services.trail.trail import _build_trail_read
from src.core.ee_hooks import notify_ee_learner_progress
from src.services.courses.certifications import check_course_completion_and_create_certificate, is_course_fully_completed, sync_trailrun_status, create_certificate_user
from src.services.email.utils import get_base_url_from_request, get_org_signup_base_url
from src.services.orgs.join_notifications import notify_user_joined_org
from src.services.orgs.users import _rights_within
from src.services.analytics.analytics import track
from src.services.analytics import events as analytics_events
from src.services.webhooks.dispatch import dispatch_webhooks
from src.security.auth import create_access_token
from src.security.org_auth import org_owns_account
from src.security.session_context import AUTH_METHOD_API_TOKEN, session_claims
from src.security.features_utils.plan_check import get_org_plan
from src.security.features_utils.plans import plan_meets_requirement
from src.security.features_utils.usage import _role_grants_dashboard_access, check_admin_seat_limit, check_limits_with_usage, decrease_feature_usage, enforce_admin_seat_limit_for_role_change, increase_feature_usage
from src.security.security import security_hash_password
from src.security.rbac.constants import ADMIN_ROLE_ID, MAINTAINER_ROLE_ID
from src.services.security.password_validation import validate_password_complexity
from src.services.security.profile_validation import validate_profile_fields

def _require_api_token(current_user) -> APITokenUser:
    """Ensure the current user is an API token."""
    if not isinstance(current_user, APITokenUser):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='This endpoint requires API token authentication')
    return current_user
_TOKEN_ACTIONS = frozenset({'create', 'read', 'update', 'delete'})

def _require_token_right(token_user, resource: str, action: str) -> None:
    """Refuse unless the token's rights grant ``action`` on ``resource``.

    Passing the org boundary only says which org a token may touch; what it
    may do there is its ``rights`` (``{"users": {"action_read": true, ...}}``,
    stored as a dict or a ``Rights`` model). A missing bucket or flag denies.
    Superadmin tokens are not org-scoped rights holders and keep full access.
    """
    if action not in _TOKEN_ACTIONS:
        raise ValueError(f'Unknown token action: {action}')
    if isinstance(token_user, SuperadminAPITokenUser):
        return
    rights = getattr(token_user, 'rights', None) or {}
    bucket = rights.get(resource) if isinstance(rights, dict) else getattr(rights, resource, None)
    key = f'action_{action}'
    if isinstance(bucket, dict):
        granted = bucket.get(key)
    else:
        granted = getattr(bucket, key, None)
    if granted is not True:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"API token does not have '{action}' permission for {resource}")

async def _resolve_org_slug(org_slug: str, token_user: APITokenUser, db_session: AsyncSession) -> Organization:
    """Resolve an org_slug, verify it matches the API token's org, and check plan."""
    org = (await db_session.execute(select(Organization).where(Organization.slug == org_slug))).scalars().first()
    if not org:
        raise HTTPException(status_code=404, detail='Organization not found')
    if org.id != token_user.org_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='API token does not have access to this organization')
    current_plan = await get_org_plan(org.id, db_session)
    if not plan_meets_requirement(current_plan, 'pro'):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Admin API requires a Pro plan or higher.')
    return org

async def _get_user_in_org(user_id: int, org_id: int, db_session: AsyncSession) -> User:
    """Get a user and verify they belong to the token's organization (single JOIN)."""
    result = (await db_session.execute(select(User).join(UserOrganization, (UserOrganization.user_id == User.id) & (UserOrganization.org_id == org_id)).where(User.id == user_id))).scalar_one_or_none()
    if result is None:
        exists = (await db_session.execute(select(User.id).where(User.id == user_id))).scalar_one_or_none()
        if not exists:
            raise HTTPException(status_code=404, detail='User not found')
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='User does not belong to this organization')
    return result
_ROLE_PRIORITY = {ADMIN_ROLE_ID: 0, MAINTAINER_ROLE_ID: 1, 3: 2, 4: 3}
_DEFAULT_ROLE_PRIORITY = 2

def _role_priority(role_id: int) -> int:
    return _ROLE_PRIORITY.get(role_id, _DEFAULT_ROLE_PRIORITY)
_TOKEN_COMPARED_BUCKETS = frozenset({'courses', 'activities', 'assignments', 'coursechapters', 'usergroups', 'users', 'roles', 'organizations'})

def _role_within_token_rights(role_rights, token_rights) -> bool:
    """Whether a role grants no write authority the token does not hold.

    Only create/update/delete are compared: read access is what every member
    has, and comparing it would stop a users-only token from serving ordinary
    learners. An ``action_x_own`` permission is covered by the token's
    ``action_x``.
    """

    def as_dict(rights) -> dict:
        if rights is None:
            return {}
        return rights if isinstance(rights, dict) else rights.model_dump()
    granted = {}
    for bucket, perms in as_dict(role_rights).items():
        if bucket not in _TOKEN_COMPARED_BUCKETS or not isinstance(perms, dict):
            continue
        granted[bucket] = {perm: value for perm, value in perms.items() if not perm.startswith('action_read')}
    held = {}
    for bucket, perms in as_dict(token_rights).items():
        if not isinstance(perms, dict):
            continue
        held[bucket] = dict(perms)
        for perm, value in perms.items():
            if value is True:
                held[bucket][f'{perm}_own'] = True
    return _rights_within(granted, held)

async def _check_token_can_impersonate(user: User, token_user: APITokenUser, db_session: AsyncSession, *, check_rights: bool=True) -> None:
    """Refuse to mint a session for a privileged account.

    Same reasoning as :func:`_check_token_can_assign_role`: elevated authority
    only comes from an interactive admin flow, so a leaked token cannot borrow
    an org Admin/Maintainer's (or a platform superadmin's) session and inherit
    every check that trusts it.
    """
    org_id = token_user.org_id
    if user.is_superadmin:
        raise HTTPException(status_code=403, detail='API tokens cannot issue tokens for superadmin accounts')
    membership = (await db_session.execute(select(UserOrganization).where(UserOrganization.user_id == user.id, UserOrganization.org_id == org_id))).scalars().first()
    if membership is not None and membership.role_id in {ADMIN_ROLE_ID, MAINTAINER_ROLE_ID}:
        raise HTTPException(status_code=403, detail='API tokens cannot issue tokens for Admin or Maintainer accounts')
    if check_rights and membership is not None:
        target_role = await db_session.get(Role, membership.role_id)
        if target_role is not None and (not _role_within_token_rights(target_role.rights, token_user.rights)):
            raise HTTPException(status_code=403, detail="API tokens cannot issue tokens for accounts with rights beyond the token's own")
    await _check_not_member_elsewhere(user, org_id, db_session, detail='API tokens cannot issue tokens for accounts that belong to other organizations')

async def _check_not_member_elsewhere(user: User, org_id: int, db_session: AsyncSession, *, detail: str) -> None:
    """Refuse when the global account is not the token org's alone."""
    if not await org_owns_account(user.id, org_id, db_session):
        raise HTTPException(status_code=403, detail=detail)

async def _check_token_can_manage_membership(user_id: int, org_id: int, db_session: AsyncSession) -> None:
    """Role changes and removals never touch an Admin/Maintainer via a token."""
    membership = (await db_session.execute(select(UserOrganization).where(UserOrganization.user_id == user_id, UserOrganization.org_id == org_id))).scalars().first()
    if membership is not None and membership.role_id in {ADMIN_ROLE_ID, MAINTAINER_ROLE_ID}:
        raise HTTPException(status_code=403, detail='API tokens cannot modify Admin or Maintainer accounts')

async def _check_token_can_edit_account(user: User, org_id: int, db_session: AsyncSession) -> None:
    """Refuse edits to a global account the token's org does not solely own.

    The ``User`` row is shared by every org it belongs to, so a token may only
    rewrite it when the account is an ordinary member of the token's org alone.
    """
    if user.is_superadmin:
        raise HTTPException(status_code=403, detail='API tokens cannot modify superadmin accounts')
    await _check_token_can_manage_membership(user.id, org_id, db_session)
    await _check_not_member_elsewhere(user, org_id, db_session, detail='API tokens cannot modify accounts that belong to other organizations')

async def _check_token_can_assign_role(token_user: APITokenUser, role: Role, db_session: AsyncSession) -> None:
    """Defense-in-depth guard for role assignment via API tokens.

    Layer 3: API tokens never grant Admin or Maintainer. Elevated roles must
    be assigned through interactive admin flows so a leaked token cannot mint
    org admins.

    Layer 2: The token's creator must still be a member of the org and must
    hold privilege at least as high as the role being granted. Stops a
    demoted/removed user's still-valid token from being used to escalate.
    """
    if role.id in {ADMIN_ROLE_ID, MAINTAINER_ROLE_ID}:
        raise HTTPException(status_code=403, detail='API tokens cannot grant Admin or Maintainer roles')
    if not _role_within_token_rights(role.rights, token_user.rights):
        raise HTTPException(status_code=403, detail="API tokens cannot grant a role with rights beyond the token's own")
    membership_q = select(UserOrganization).where(UserOrganization.user_id == token_user.created_by_user_id, UserOrganization.org_id == token_user.org_id)
    creator_membership = (await db_session.execute(membership_q)).scalars().first()
    if creator_membership is None:
        raise HTTPException(status_code=403, detail='Token creator is no longer a member of this organization')
    if _role_priority(role.id) < _role_priority(creator_membership.role_id):
        raise HTTPException(status_code=403, detail='Token cannot grant a role with higher privilege than its creator')

async def issue_user_token(token_user: APITokenUser, user_id: int, db_session: AsyncSession) -> dict:
    """Issue a JWT access token on behalf of a user in the token's org.

    Refuses privileged targets: without that, any token, whatever its rights,
    could mint a full session for the org's administrator and inherit every
    permission the token itself was never granted.

    Minting a session acts as the user, so the token needs ``users.action_update``
    on top of the privileged-target refusal.
    """
    _require_token_right(token_user, 'users', 'update')
    user = await _get_user_in_org(user_id, token_user.org_id, db_session)
    await _check_token_can_impersonate(user, token_user, db_session)
    from datetime import timedelta
    access_token = create_access_token(data={'sub': user.email, 'purpose': 'session', **session_claims(AUTH_METHOD_API_TOKEN, token_user.org_id)}, expires_delta=timedelta(hours=1))
    from src.services.auth.session import track_login
    await track_login(user.id, AUTH_METHOD_API_TOKEN, token_user.org_id)
    return {'access_token': access_token, 'token_type': 'bearer', 'user_id': user.id, 'user_uuid': user.user_uuid}

async def check_course_access(token_user: APITokenUser, course_uuid: str, user_id: int, db_session: AsyncSession) -> dict:
    """Check if a user can access a specific course within the token's org."""
    _require_token_right(token_user, 'courses', 'read')
    await _get_user_in_org(user_id, token_user.org_id, db_session)
    course = (await db_session.execute(select(Course).where(Course.course_uuid == course_uuid, Course.org_id == token_user.org_id))).scalars().first()
    if not course:
        raise HTTPException(status_code=404, detail='Course not found')
    enrollment = (await db_session.execute(select(TrailRun).where(TrailRun.course_id == course.id, TrailRun.user_id == user_id, TrailRun.org_id == token_user.org_id))).scalars().first()
    return {'has_access': course.public or enrollment is not None, 'is_enrolled': enrollment is not None, 'is_public': course.public, 'is_published': course.published}

async def enroll_user(request: Request, token_user: APITokenUser, user_id: int, course_uuid: str, db_session: AsyncSession) -> TrailRead:
    """Enroll a user in a course on their behalf."""
    _require_token_right(token_user, 'courses', 'update')
    await _get_user_in_org(user_id, token_user.org_id, db_session)
    course = (await db_session.execute(select(Course).where(Course.course_uuid == course_uuid, Course.org_id == token_user.org_id))).scalars().first()
    if not course:
        raise HTTPException(status_code=404, detail='Course not found')
    trail = (await db_session.execute(select(Trail).where(Trail.org_id == token_user.org_id, Trail.user_id == user_id))).scalars().first()
    if not trail:
        trail = Trail(org_id=token_user.org_id, user_id=user_id, trail_uuid=f'trail_{uuid4()}', creation_date=str(datetime.now()), update_date=str(datetime.now()))
        db_session.add(trail)
        await db_session.commit()
        await db_session.refresh(trail)
    existing = (await db_session.execute(select(TrailRun).where(TrailRun.course_id == course.id, TrailRun.user_id == user_id, TrailRun.org_id == token_user.org_id))).scalars().first()
    if existing:
        raise HTTPException(status_code=400, detail='User is already enrolled in this course')
    trail_run = TrailRun(trail_id=trail.id if trail.id is not None else 0, course_id=course.id if course.id is not None else 0, org_id=token_user.org_id, user_id=user_id, creation_date=str(datetime.now()), update_date=str(datetime.now()))
    db_session.add(trail_run)
    await db_session.commit()
    await db_session.refresh(trail_run)
    await track(event_name=analytics_events.COURSE_ENROLLED, org_id=token_user.org_id, user_id=user_id, properties={'course_uuid': course.course_uuid})
    trail_runs_raw = list((await db_session.execute(select(TrailRun).where(TrailRun.trail_id == trail.id, TrailRun.user_id == user_id))).scalars().all())
    return await _build_trail_read(trail, trail_runs_raw, db_session, user_id=user_id)

async def unenroll_user(token_user: APITokenUser, user_id: int, course_uuid: str, db_session: AsyncSession) -> dict:
    """Unenroll a user from a course."""
    _require_token_right(token_user, 'courses', 'update')
    await _get_user_in_org(user_id, token_user.org_id, db_session)
    course = (await db_session.execute(select(Course).where(Course.course_uuid == course_uuid, Course.org_id == token_user.org_id))).scalars().first()
    if not course:
        raise HTTPException(status_code=404, detail='Course not found')
    trail_run = (await db_session.execute(select(TrailRun).where(TrailRun.course_id == course.id, TrailRun.user_id == user_id, TrailRun.org_id == token_user.org_id))).scalars().first()
    if not trail_run:
        raise HTTPException(status_code=404, detail='Enrollment not found')
    steps = (await db_session.execute(select(TrailStep).where(TrailStep.course_id == course.id, TrailStep.user_id == user_id, TrailStep.org_id == token_user.org_id))).scalars().all()
    for step in steps:
        await db_session.delete(step)
    await db_session.delete(trail_run)
    await db_session.commit()
    notify_ee_learner_progress(user_id, course.id)
    await track(event_name=analytics_events.COURSE_LEFT, org_id=token_user.org_id, user_id=user_id, properties={'course_uuid': course.course_uuid})
    return {'detail': 'User unenrolled successfully'}

async def get_user_enrollments(token_user: APITokenUser, user_id: int, db_session: AsyncSession) -> TrailRead:
    """Get all enrollments for a user in the token's org."""
    _require_token_right(token_user, 'courses', 'read')
    await _get_user_in_org(user_id, token_user.org_id, db_session)
    trail = (await db_session.execute(select(Trail).where(Trail.org_id == token_user.org_id, Trail.user_id == user_id))).scalars().first()
    if not trail:
        return TrailRead(org_id=token_user.org_id, user_id=user_id, runs=[])
    trail_runs_raw = list((await db_session.execute(select(TrailRun).where(TrailRun.trail_id == trail.id, TrailRun.user_id == user_id))).scalars().all())
    return await _build_trail_read(trail, trail_runs_raw, db_session, user_id=user_id)

async def get_user_progress(token_user: APITokenUser, user_id: int, course_uuid: str, db_session: AsyncSession) -> dict:
    """Get a user's progress in a specific course."""
    _require_token_right(token_user, 'courses', 'read')
    await _get_user_in_org(user_id, token_user.org_id, db_session)
    course = (await db_session.execute(select(Course).where(Course.course_uuid == course_uuid, Course.org_id == token_user.org_id))).scalars().first()
    if not course:
        raise HTTPException(status_code=404, detail='Course not found')
    total = (await db_session.execute(select(func.count(ChapterActivity.id)).where(ChapterActivity.course_id == course.id))).scalar_one()
    completed_activity_ids = (await db_session.execute(select(TrailStep.activity_id).where(TrailStep.user_id == user_id, TrailStep.course_id == course.id, TrailStep.complete == True))).scalars().all()
    return {'course_uuid': course.course_uuid, 'user_id': user_id, 'total_activities': total, 'completed_activities': len(completed_activity_ids), 'completion_percentage': round(len(completed_activity_ids) / total * 100, 1) if total > 0 else 0, 'completed_activity_ids': completed_activity_ids}

async def complete_activity(request: Request, token_user: APITokenUser, user_id: int, activity_uuid: str, db_session: AsyncSession) -> dict:
    """Mark an activity as completed on behalf of a user."""
    _require_token_right(token_user, 'courses', 'update')
    await _get_user_in_org(user_id, token_user.org_id, db_session)
    activity = (await db_session.execute(select(Activity).where(Activity.activity_uuid == activity_uuid))).scalars().first()
    if not activity:
        raise HTTPException(status_code=404, detail='Activity not found')
    course = (await db_session.execute(select(Course).where(Course.id == activity.course_id))).scalars().first()
    if not course or course.org_id != token_user.org_id:
        raise HTTPException(status_code=404, detail='Activity not found')
    trail = (await db_session.execute(select(Trail).where(Trail.org_id == token_user.org_id, Trail.user_id == user_id))).scalars().first()
    if not trail:
        trail = Trail(org_id=token_user.org_id, user_id=user_id, trail_uuid=f'trail_{uuid4()}', creation_date=str(datetime.now()), update_date=str(datetime.now()))
        db_session.add(trail)
        await db_session.commit()
        await db_session.refresh(trail)
    trailrun = (await db_session.execute(select(TrailRun).where(TrailRun.trail_id == trail.id, TrailRun.course_id == course.id, TrailRun.user_id == user_id))).scalars().first()
    if not trailrun:
        trailrun = TrailRun(trail_id=trail.id if trail.id is not None else 0, course_id=course.id if course.id is not None else 0, org_id=course.org_id, user_id=user_id, creation_date=str(datetime.now()), update_date=str(datetime.now()))
        db_session.add(trailrun)
        await db_session.commit()
        await db_session.refresh(trailrun)
    existing_step = (await db_session.execute(select(TrailStep).where(TrailStep.trailrun_id == trailrun.id, TrailStep.activity_id == activity.id, TrailStep.user_id == user_id))).scalars().first()
    is_new = existing_step is None
    if is_new:
        step = TrailStep(trailrun_id=trailrun.id if trailrun.id is not None else 0, activity_id=activity.id if activity.id is not None else 0, course_id=course.id if course.id is not None else 0, trail_id=trail.id if trail.id is not None else 0, org_id=course.org_id, complete=True, teacher_verified=False, grade='', user_id=user_id, creation_date=str(datetime.now()), update_date=str(datetime.now()))
        db_session.add(step)
        await db_session.commit()
        await db_session.refresh(step)
        await track(event_name=analytics_events.ACTIVITY_COMPLETED, org_id=course.org_id, user_id=user_id, properties={'activity_uuid': activity_uuid, 'course_uuid': course.course_uuid, 'activity_type': activity.activity_type if activity.activity_type else ''})
    course_completed = False
    if course.id:
        await check_course_completion_and_create_certificate(request, user_id, course.id, db_session)
        course_completed = await is_course_fully_completed(user_id, course.id, db_session)
        await sync_trailrun_status(user_id, course.id, db_session)
    if course_completed:
        await track(event_name=analytics_events.COURSE_COMPLETED, org_id=course.org_id, user_id=user_id, properties={'course_uuid': course.course_uuid})
    notify_ee_learner_progress(user_id, course.id)
    return {'activity_uuid': activity_uuid, 'user_id': user_id, 'completed': True, 'is_new_completion': is_new, 'course_completed': course_completed}

async def uncomplete_activity(token_user: APITokenUser, user_id: int, activity_uuid: str, db_session: AsyncSession) -> dict:
    """Remove an activity completion for a user."""
    _require_token_right(token_user, 'courses', 'update')
    await _get_user_in_org(user_id, token_user.org_id, db_session)
    activity = (await db_session.execute(select(Activity).where(Activity.activity_uuid == activity_uuid))).scalars().first()
    if not activity:
        raise HTTPException(status_code=404, detail='Activity not found')
    course = (await db_session.execute(select(Course).where(Course.id == activity.course_id))).scalars().first()
    if not course or course.org_id != token_user.org_id:
        raise HTTPException(status_code=404, detail='Activity not found')
    step = (await db_session.execute(select(TrailStep).where(TrailStep.activity_id == activity.id, TrailStep.user_id == user_id, TrailStep.org_id == token_user.org_id))).scalars().first()
    if step:
        await db_session.delete(step)
        await db_session.commit()
        if course.id:
            await sync_trailrun_status(user_id, course.id, db_session)
        notify_ee_learner_progress(user_id, course.id)
    return {'activity_uuid': activity_uuid, 'user_id': user_id, 'completed': False}

async def complete_course(request: Request, token_user: APITokenUser, user_id: int, course_uuid: str, db_session: AsyncSession) -> dict:
    """Mark all activities in a course as completed for a user."""
    _require_token_right(token_user, 'courses', 'update')
    await _get_user_in_org(user_id, token_user.org_id, db_session)
    course = (await db_session.execute(select(Course).where(Course.course_uuid == course_uuid, Course.org_id == token_user.org_id))).scalars().first()
    if not course:
        raise HTTPException(status_code=404, detail='Course not found')
    trail = (await db_session.execute(select(Trail).where(Trail.org_id == token_user.org_id, Trail.user_id == user_id))).scalars().first()
    if not trail:
        trail = Trail(org_id=token_user.org_id, user_id=user_id, trail_uuid=f'trail_{uuid4()}', creation_date=str(datetime.now()), update_date=str(datetime.now()))
        db_session.add(trail)
        await db_session.commit()
        await db_session.refresh(trail)
    trailrun = (await db_session.execute(select(TrailRun).where(TrailRun.trail_id == trail.id, TrailRun.course_id == course.id, TrailRun.user_id == user_id))).scalars().first()
    if not trailrun:
        trailrun = TrailRun(trail_id=trail.id if trail.id is not None else 0, course_id=course.id if course.id is not None else 0, org_id=course.org_id, user_id=user_id, creation_date=str(datetime.now()), update_date=str(datetime.now()))
        db_session.add(trailrun)
        await db_session.commit()
        await db_session.refresh(trailrun)
    chapter_activities = (await db_session.execute(select(ChapterActivity).where(ChapterActivity.course_id == course.id))).scalars().all()
    activity_ids = [ca.activity_id for ca in chapter_activities]
    if not activity_ids:
        return {'course_uuid': course_uuid, 'user_id': user_id, 'completed_count': 0, 'already_completed_count': 0, 'total_activities': 0, 'course_completed': False, 'certificate_awarded': False}
    existing_steps = (await db_session.execute(select(TrailStep).where(TrailStep.user_id == user_id, TrailStep.course_id == course.id, TrailStep.complete == True))).scalars().all()
    already_completed = {s.activity_id for s in existing_steps}
    new_count = 0
    for activity_id in activity_ids:
        if activity_id not in already_completed:
            step = TrailStep(trailrun_id=trailrun.id if trailrun.id is not None else 0, activity_id=activity_id, course_id=course.id if course.id is not None else 0, trail_id=trail.id if trail.id is not None else 0, org_id=course.org_id, complete=True, teacher_verified=False, grade='', user_id=user_id, creation_date=str(datetime.now()), update_date=str(datetime.now()))
            db_session.add(step)
            new_count += 1
    await db_session.commit()
    certificate_awarded = await check_course_completion_and_create_certificate(request, user_id, course.id, db_session)
    course_completed = await is_course_fully_completed(user_id, course.id, db_session)
    await sync_trailrun_status(user_id, course.id, db_session)
    if course_completed:
        await track(event_name=analytics_events.COURSE_COMPLETED, org_id=course.org_id, user_id=user_id, properties={'course_uuid': course.course_uuid})
    notify_ee_learner_progress(user_id, course.id)
    return {'course_uuid': course_uuid, 'user_id': user_id, 'completed_count': new_count, 'already_completed_count': len(already_completed), 'total_activities': len(activity_ids), 'course_completed': course_completed, 'certificate_awarded': certificate_awarded}

async def get_all_user_progress(token_user: APITokenUser, user_id: int, db_session: AsyncSession) -> List[dict]:
    """Get progress summary for all courses a user is enrolled in."""
    _require_token_right(token_user, 'courses', 'read')
    await _get_user_in_org(user_id, token_user.org_id, db_session)
    trail_runs = (await db_session.execute(select(TrailRun).where(TrailRun.user_id == user_id, TrailRun.org_id == token_user.org_id))).scalars().all()
    if not trail_runs:
        return []
    course_ids = [tr.course_id for tr in trail_runs]
    courses = (await db_session.execute(select(Course).where(Course.id.in_(course_ids)))).scalars().all()
    course_map = {c.id: c for c in courses}
    total_counts = (await db_session.execute(select(ChapterActivity.course_id, func.count(ChapterActivity.id)).where(ChapterActivity.course_id.in_(course_ids)).group_by(ChapterActivity.course_id))).all()
    total_map = {row[0]: row[1] for row in total_counts}
    completed_counts = (await db_session.execute(select(TrailStep.course_id, func.count(TrailStep.id)).where(TrailStep.user_id == user_id, TrailStep.course_id.in_(course_ids), TrailStep.complete == True).group_by(TrailStep.course_id))).all()
    completed_map = {row[0]: row[1] for row in completed_counts}
    result = []
    for tr in trail_runs:
        course = course_map.get(tr.course_id)
        if not course:
            continue
        total = total_map.get(tr.course_id, 0)
        completed = completed_map.get(tr.course_id, 0)
        result.append({'course_uuid': course.course_uuid, 'course_name': course.name, 'status': tr.status.value, 'total_activities': total, 'completed_activities': completed, 'completion_percentage': round(completed / total * 100, 1) if total > 0 else 0, 'enrolled_at': tr.creation_date})
    return result

def _enum_value(value) -> str:
    return value.value if hasattr(value, 'value') else str(value)

async def get_user_trail_detail(token_user: APITokenUser, user_id: int, db_session: AsyncSession, course_uuid: Optional[str]=None) -> dict:
    """Build a full trail breakdown for a user: every chapter + every activity
    with per-activity completion status. Optionally filtered to a single course."""
    _require_token_right(token_user, 'courses', 'read')
    await _get_user_in_org(user_id, token_user.org_id, db_session)
    target_course: Optional[Course] = None
    if course_uuid is not None:
        target_course = (await db_session.execute(select(Course).where(Course.course_uuid == course_uuid, Course.org_id == token_user.org_id))).scalars().first()
        if not target_course:
            raise HTTPException(status_code=404, detail='Course not found')
    trail = (await db_session.execute(select(Trail).where(Trail.org_id == token_user.org_id, Trail.user_id == user_id))).scalars().first()
    empty_shell = {'trail_uuid': trail.trail_uuid if trail else None, 'user_id': user_id, 'org_id': token_user.org_id, 'creation_date': trail.creation_date if trail else None, 'update_date': trail.update_date if trail else None, 'courses': []}
    tr_statement = select(TrailRun).where(TrailRun.user_id == user_id, TrailRun.org_id == token_user.org_id)
    if trail:
        tr_statement = tr_statement.where(TrailRun.trail_id == trail.id)
    if target_course is not None:
        tr_statement = tr_statement.where(TrailRun.course_id == target_course.id)
    trail_runs = list((await db_session.execute(tr_statement)).scalars().all())
    course_ids: list[int] = [tr.course_id for tr in trail_runs]
    if target_course is not None and target_course.id not in course_ids:
        course_ids.append(target_course.id)
    if not course_ids:
        return empty_shell
    courses = (await db_session.execute(select(Course).where(Course.id.in_(course_ids)))).scalars().all()
    course_map = {c.id: c for c in courses if c.org_id == token_user.org_id}
    trail_runs = [tr for tr in trail_runs if tr.course_id in course_map]
    run_by_course: dict[int, TrailRun] = {tr.course_id: tr for tr in trail_runs}
    course_chapters = (await db_session.execute(select(CourseChapter).where(CourseChapter.course_id.in_(course_map.keys())))).scalars().all()
    chapters_by_course: dict[int, list[tuple[int, int]]] = {}
    for cc in course_chapters:
        chapters_by_course.setdefault(cc.course_id, []).append((cc.chapter_id, cc.order))
    for cid in chapters_by_course:
        chapters_by_course[cid].sort(key=lambda t: t[1])
    chapter_ids = [cc.chapter_id for cc in course_chapters]
    chapter_map: dict[int, Chapter] = {}
    if chapter_ids:
        chapters = (await db_session.execute(select(Chapter).where(Chapter.id.in_(chapter_ids)))).scalars().all()
        chapter_map = {c.id: c for c in chapters}
    chapter_activities = (await db_session.execute(select(ChapterActivity).where(ChapterActivity.course_id.in_(course_map.keys())))).scalars().all()
    chapter_activities_by_chapter: dict[int, list[ChapterActivity]] = {}
    for ca in chapter_activities:
        chapter_activities_by_chapter.setdefault(ca.chapter_id, []).append(ca)
    for cid in chapter_activities_by_chapter:
        chapter_activities_by_chapter[cid].sort(key=lambda a: a.order)
    activity_ids = list({ca.activity_id for ca in chapter_activities})
    activity_map: dict[int, Activity] = {}
    if activity_ids:
        activities = (await db_session.execute(select(Activity).where(Activity.id.in_(activity_ids)))).scalars().all()
        activity_map = {a.id: a for a in activities}
    trail_steps = (await db_session.execute(select(TrailStep).where(TrailStep.user_id == user_id, TrailStep.course_id.in_(course_map.keys())))).scalars().all()
    step_by_activity: dict[int, TrailStep] = {s.activity_id: s for s in trail_steps}
    course_blocks: list[dict] = []
    for course_id, course in course_map.items():
        run = run_by_course.get(course_id)
        chapter_blocks: list[dict] = []
        course_total = 0
        course_completed = 0
        for chapter_id, chapter_order in chapters_by_course.get(course_id, []):
            chapter = chapter_map.get(chapter_id)
            if not chapter:
                continue
            activity_blocks: list[dict] = []
            chap_total = 0
            chap_completed = 0
            for ca in chapter_activities_by_chapter.get(chapter_id, []):
                act = activity_map.get(ca.activity_id)
                if not act:
                    continue
                step = step_by_activity.get(act.id)
                completed = bool(step and step.complete)
                chap_total += 1
                if completed:
                    chap_completed += 1
                activity_blocks.append({'activity_uuid': act.activity_uuid, 'activity_id': act.id, 'name': act.name, 'activity_type': _enum_value(act.activity_type), 'activity_sub_type': _enum_value(act.activity_sub_type), 'order': ca.order, 'published': act.published, 'completed': completed, 'teacher_verified': bool(step.teacher_verified) if step else False, 'grade': step.grade if step else '', 'completed_at': step.update_date if step and step.complete else None})
            chapter_blocks.append({'chapter_uuid': chapter.chapter_uuid, 'chapter_id': chapter.id, 'name': chapter.name, 'order': chapter_order, 'total_activities': chap_total, 'completed_activities': chap_completed, 'activities': activity_blocks})
            course_total += chap_total
            course_completed += chap_completed
        course_blocks.append({'course_uuid': course.course_uuid, 'course_id': course.id, 'course_name': course.name, 'status': _enum_value(run.status) if run else None, 'enrolled_at': run.creation_date if run else None, 'total_activities': course_total, 'completed_activities': course_completed, 'completion_percentage': round(course_completed / course_total * 100, 1) if course_total > 0 else 0, 'chapters': chapter_blocks})
    return {'trail_uuid': trail.trail_uuid if trail else None, 'user_id': user_id, 'org_id': token_user.org_id, 'creation_date': trail.creation_date if trail else None, 'update_date': trail.update_date if trail else None, 'courses': course_blocks}

async def provision_user(token_user: APITokenUser, email: str, username: str, first_name: str, last_name: str, password: Optional[str], role_id: int, request: Request, db_session: AsyncSession, extra_metadata: Optional[dict]=None) -> UserRead:
    """Create a user and attach them to the token's org in one call.

    Designed for SSO/JIT provisioning: email is auto-verified and the user
    bypasses the normal email-verification flow.
    """
    _require_token_right(token_user, 'users', 'create')
    name_check = validate_profile_fields({'username': username, 'first_name': first_name, 'last_name': last_name})
    if not name_check.is_valid:
        raise HTTPException(status_code=400, detail={'code': 'PROFILE_FIELD_INVALID', 'message': 'Display name fields may not contain URLs or links', 'errors': name_check.errors, 'invalid_fields': name_check.invalid_fields})
    if password:
        validation = validate_password_complexity(password)
        if not validation.is_valid:
            raise HTTPException(status_code=400, detail={'code': 'WEAK_PASSWORD', 'message': 'Password does not meet security requirements', 'errors': validation.errors})
    role = (await db_session.execute(select(Role).where(Role.id == role_id))).scalars().first()
    if not role:
        raise HTTPException(status_code=400, detail='Role not found')
    if role.org_id is not None and role.org_id != token_user.org_id:
        raise HTTPException(status_code=403, detail='Role does not belong to this organization')
    await _check_token_can_assign_role(token_user, role, db_session)
    await check_limits_with_usage('members', token_user.org_id, db_session)
    if _role_grants_dashboard_access(role):
        await check_admin_seat_limit(token_user.org_id, db_session)
    now = datetime.now()
    existing_user = (await db_session.execute(select(User).where(User.email == email))).scalars().first()
    if existing_user:
        raise HTTPException(status_code=409, detail='A user with this email already exists; invite them instead')
    if (await db_session.execute(select(User).where(User.username == username))).scalars().first():
        raise HTTPException(status_code=400, detail='Username already exists')
    user = User(username=username, email=email, first_name=first_name, last_name=last_name, password=security_hash_password(password) if password else '', user_uuid=f'user_{uuid4()}', email_verified=True, email_verified_at=now.isoformat(), signup_method='admin_api', creation_date=str(now), update_date=str(now), extra_metadata=extra_metadata)
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    membership = UserOrganization(user_id=user.id if user.id else 0, org_id=token_user.org_id, role_id=role_id, creation_date=str(now), update_date=str(now))
    db_session.add(membership)
    await db_session.commit()
    await increase_feature_usage('members', token_user.org_id, db_session)
    await track(event_name=analytics_events.USER_SIGNED_UP, org_id=token_user.org_id, user_id=user.id if user.id else 0, properties={'signup_method': 'admin_api'})
    await track(event_name=analytics_events.USER_JOINED_ORG, org_id=token_user.org_id, user_id=user.id if user.id else 0, properties={'join_method': 'admin_api'})
    await dispatch_webhooks(event_name=analytics_events.USER_SIGNED_UP, org_id=token_user.org_id, data={'user': {'user_uuid': user.user_uuid, 'email': user.email, 'username': user.username, 'first_name': user.first_name, 'last_name': user.last_name}, 'signup_method': 'admin_api'})
    await notify_user_joined_org(request, db_session, user, token_user.org_id)
    return UserRead.model_validate(user)

async def remove_user_from_org_admin(token_user: APITokenUser, user_id: int, db_session: AsyncSession) -> dict:
    """Remove a user's org membership (scope: membership only)."""
    _require_token_right(token_user, 'users', 'delete')
    await _get_user_in_org(user_id, token_user.org_id, db_session)
    await _check_token_can_manage_membership(user_id, token_user.org_id, db_session)
    membership = (await db_session.execute(select(UserOrganization).where(UserOrganization.user_id == user_id, UserOrganization.org_id == token_user.org_id))).scalars().first()
    if not membership:
        raise HTTPException(status_code=404, detail='User not in org')
    admin_memberships = (await db_session.execute(select(UserOrganization).where(UserOrganization.org_id == token_user.org_id, UserOrganization.role_id == ADMIN_ROLE_ID))).scalars().all()
    if len(admin_memberships) == 1 and admin_memberships[0].user_id == user_id:
        raise HTTPException(status_code=400, detail='Cannot remove the last admin of the organization')
    await db_session.delete(membership)
    await db_session.commit()
    try:
        from src.routers.users import _invalidate_session_cache
        _invalidate_session_cache(user_id)
    except Exception:
        pass
    try:
        await decrease_feature_usage('members', token_user.org_id, db_session)
    except Exception:
        pass
    await track(event_name=analytics_events.USER_REMOVED_FROM_ORG, org_id=token_user.org_id, user_id=user_id)
    await dispatch_webhooks(event_name=analytics_events.USER_REMOVED_FROM_ORG, org_id=token_user.org_id, data={'user_id': user_id, 'org_id': token_user.org_id})
    return {'detail': 'User removed from org'}

async def get_user_by_email(token_user: APITokenUser, email: str, db_session: AsyncSession) -> OrgMemberUserRead:
    """Find a user by email within the token's org. 404 if not a member."""
    _require_token_right(token_user, 'users', 'read')
    row = (await db_session.execute(select(User).join(UserOrganization, UserOrganization.user_id == User.id).where(User.email == email, UserOrganization.org_id == token_user.org_id))).scalars().first()
    if not row:
        raise HTTPException(status_code=404, detail='User not found in this organization')
    meta_keys = await _org_signup_field_keys(token_user.org_id, db_session)
    return OrgMemberUserRead.for_org(row, meta_keys)

def _validate_magic_link_redirect(redirect_to: Optional[str]) -> Optional[str]:
    """Validate that a magic-link redirect_to is a same-origin path.

    Rejects anything that contains a scheme or looks like a protocol-relative
    URL. Only allows paths like "/course/foo" that resolve on the same host
    the consume endpoint was hit on. Prevents open-redirect phishing.
    """
    if redirect_to is None or redirect_to == '':
        return None
    value = redirect_to.strip()
    if not value:
        return None
    from urllib.parse import urlsplit
    detail = "redirect_to must be a same-origin path starting with '/'"
    if not value.startswith('/'):
        raise HTTPException(status_code=400, detail=detail)
    if len(value) >= 2 and value[1] in ('/', '\\'):
        raise HTTPException(status_code=400, detail=detail)
    parts = urlsplit(value.replace('\\', '/'))
    if parts.scheme or parts.netloc:
        raise HTTPException(status_code=400, detail=detail)
    return value

def _magic_link_error(status_code: int, code: str, message: str) -> HTTPException:
    """A refusal in the shape the /auth/magic page reads: ``{"code", "message"}``,
    the same shape the emailed login link uses, so both kinds of link fail the
    same way on screen."""
    return HTTPException(status_code=status_code, detail={'code': code, 'message': message})

def _magic_link_landing_url(base: str, token: str, redirect_to: Optional[str]) -> str:
    """The web page that redeems a sign-in link: ``{base}/auth/magic?token=…``.

    Redemption happens in the web app (the same page emailed login links use),
    not on an API redirect, because only the web app sets the session cookies
    from the host the browser is actually on: the token cookies plus the
    ``LH_session`` marker the client needs to see the session, scoped right on
    org subdomains and custom domains alike. Opening the page does not spend
    the link; its script does, so link previewers and mail scanners can't.
    """
    url = f"{base.rstrip('/')}/auth/magic?token={quote(token, safe='')}"
    if redirect_to:
        url += f"&next={quote(redirect_to, safe='')}"
    return url

async def _magic_link_base_url(org: Organization, request: Request, db_session: AsyncSession, host: Optional[str]=None) -> str:
    """The org's own frontend: its verified custom domain, else ``{slug}.{domain}``.

    Never the request's own host: integrations call this server to server, so
    the request carries no Origin and that fallback is the platform apex, where
    the student lands outside their org.

    ``host`` lets the integration name one of the org's verified custom domains
    outright. A domain behind a CDN fails the live DNS check behind
    ``get_org_signup_base_url`` (it resolves to the CDN), and nothing the
    platform can probe tells a CDN from a server relaying to us, so this is the
    org's explicit choice rather than a guess. It adds no exposure: the
    integration receives the raw token anyway and could put it on any host.
    """
    if host:
        from config.config import get_learnhouse_config
        from src.db.custom_domains import CustomDomain
        wanted = host.strip().lower().rstrip('.')
        domain = (await db_session.execute(select(CustomDomain).where(CustomDomain.org_id == org.id, CustomDomain.domain == wanted, CustomDomain.status == 'verified'))).scalars().first()
        if domain is None:
            raise HTTPException(status_code=400, detail="host must be one of the organization's verified custom domains")
        from src.services.orgs.custom_domains import _org_plan_includes_custom_domains
        if not await _org_plan_includes_custom_domains(org.id, db_session):
            raise HTTPException(status_code=400, detail="The organization's plan no longer includes custom domains")
        scheme = 'https' if get_learnhouse_config().hosting_config.ssl else 'http'
        return f'{scheme}://{domain.domain}'
    return await get_org_signup_base_url(org.slug, request, db_session=db_session, org_id=org.id) or get_base_url_from_request(request)

async def issue_magic_link(token_user: APITokenUser, user_id: int, redirect_to: Optional[str], ttl_seconds: int, org_slug: str, request: Request, db_session: AsyncSession, host: Optional[str]=None) -> dict:
    """Issue a short-lived, single-use JWT wrapped in a browser-clickable URL
    on the org's own frontend (see ``_magic_link_base_url``)."""
    _require_token_right(token_user, 'users', 'update')
    user = await _get_user_in_org(user_id, token_user.org_id, db_session)
    await _check_token_can_impersonate(user, token_user, db_session)
    safe_redirect = _validate_magic_link_redirect(redirect_to)
    ttl = max(60, min(ttl_seconds, 900))
    expires_delta = timedelta(seconds=ttl)
    import secrets as _secrets
    jti = _secrets.token_urlsafe(16)
    payload = {'sub': user.email, 'purpose': 'magic_link', 'org_id': token_user.org_id, 'redirect_to': safe_redirect or '', 'jti': jti, 'tid': token_user.id}
    token = create_access_token(data=payload, expires_delta=expires_delta)
    org = await db_session.get(Organization, token_user.org_id)
    base = await _magic_link_base_url(org, request, db_session, host=host)
    url = _magic_link_landing_url(base, token, safe_redirect)
    expires_at = (datetime.now() + expires_delta).isoformat()
    return {'url': url, 'token': token, 'expires_at': expires_at}

async def magic_link_landing_url_for_token(token: str, org_slug: str, request: Request, db_session: AsyncSession) -> str:
    """Where the legacy API consume URL forwards a link, without spending it.

    Links issued before redemption moved to the web app point at the API
    endpoint; they are sent on to the redeeming page on the org's own frontend.
    The org comes from the signed token. Only a token that does not verify
    (expired, garbage) falls back to the slug in the URL, so the page can show
    its usual "link not valid" state on the right site.
    """
    from src.security.auth import decode_jwt
    from src.services.email.utils import get_platform_base_url
    payload = decode_jwt(token)
    is_link = bool(payload) and payload.get('purpose') == 'magic_link'
    redirect_to = None
    if is_link:
        try:
            redirect_to = _validate_magic_link_redirect(payload.get('redirect_to') or None)
        except HTTPException:
            redirect_to = None
    org = None
    if is_link:
        org_id = payload.get('org_id')
        if isinstance(org_id, int):
            org = await db_session.get(Organization, org_id)
    elif not payload:
        org = (await db_session.execute(select(Organization).where(Organization.slug == org_slug))).scalars().first()
    base = await _magic_link_base_url(org, request, db_session) if org else get_platform_base_url(request)
    return _magic_link_landing_url(base, token, redirect_to)

async def _recheck_magic_link_issuer(token_id, user: User, org_id: int, db_session: AsyncSession) -> None:
    """Refuse a link whose issuing API token may no longer sign ``user`` in."""
    from src.security.auth import api_token_user_from
    from src.services.api_tokens.api_tokens import get_live_api_token
    refused = _magic_link_error(410, 'MAGIC_LINK_INVALID', 'This sign-in link is no longer valid. Please request a new one.')
    if token_id is None:
        token_user = APITokenUser(org_id=org_id)
        try:
            await _check_token_can_impersonate(user, token_user, db_session, check_rights=False)
        except HTTPException:
            raise refused
        return
    api_token = await get_live_api_token(token_id, db_session) if type(token_id) is int else None
    if api_token is None or api_token.org_id != org_id:
        raise refused
    try:
        await _check_token_can_impersonate(user, api_token_user_from(api_token), db_session)
    except HTTPException:
        raise refused

async def consume_magic_link_token(token: str, db_session: AsyncSession) -> tuple[User, Optional[str], Optional[str], Optional[str], Optional[str]]:
    """Validate and spend a magic-link JWT.

    Returns ``(user, access_token, refresh_token, redirect_to, mfa_token)``.
    Exactly one of ``access_token``/``mfa_token`` is populated: when the user has
    two-factor enabled the link buys a second-factor challenge, not a session.

    Raises HTTPException on an expired, invalid, incomplete or already-used
    token, or if the user is no longer a member of the org encoded in the token.
    Re-validates ``redirect_to`` defense-in-depth even though it was validated
    at issue time.
    """
    from src.security.auth import decode_jwt
    from src.services.auth.link_tokens import redeem_link_token
    from src.services.auth.session import issue_session_or_challenge
    try:
        payload = decode_jwt(token)
    except Exception:
        payload = None
    if not payload:
        raise _magic_link_error(401, 'MAGIC_LINK_INVALID', 'This sign-in link is invalid or has expired.')
    if payload.get('purpose') != 'magic_link' or payload.get('type') != 'access':
        raise _magic_link_error(410, 'MAGIC_LINK_INVALID', 'This is not a valid sign-in link.')
    email = payload.get('sub')
    org_id = payload.get('org_id')
    raw_redirect = payload.get('redirect_to') or None
    jti = payload.get('jti')
    exp = payload.get('exp')
    if not email or not org_id or (not jti) or (not isinstance(exp, int)):
        raise _magic_link_error(410, 'MAGIC_LINK_INVALID', 'This is not a valid sign-in link.')
    if not await redeem_link_token(db_session, jti, 'magic_link', exp):
        raise _magic_link_error(410, 'MAGIC_LINK_USED', 'This sign-in link has already been used. Please request a new one.')
    try:
        redirect_to = _validate_magic_link_redirect(raw_redirect)
    except HTTPException:
        redirect_to = None
    user = (await db_session.execute(select(User).where(User.email == email))).scalars().first()
    if not user:
        raise _magic_link_error(410, 'MAGIC_LINK_INVALID', 'This sign-in link is for a user that no longer exists.')
    membership = (await db_session.execute(select(UserOrganization).where(UserOrganization.user_id == user.id, UserOrganization.org_id == org_id))).scalars().first()
    if not membership:
        raise _magic_link_error(410, 'MAGIC_LINK_INVALID', 'This sign-in link is for a user who is no longer a member of this organization.')
    await _recheck_magic_link_issuer(payload.get('tid'), user, org_id, db_session)
    issue = await issue_session_or_challenge(db_session, user, amr=AUTH_METHOD_API_TOKEN, org_id=org_id)
    if issue.mfa_required:
        return (user, None, None, redirect_to, issue.mfa_token)
    return (user, issue.access_token, issue.refresh_token, redirect_to, None)

async def bulk_enroll_users(token_user: APITokenUser, course_uuid: str, user_ids: List[int], request: Request, db_session: AsyncSession) -> dict:
    """Enroll a batch of users in a course. Returns summary of results."""
    _require_token_right(token_user, 'courses', 'update')
    course = (await db_session.execute(select(Course).where(Course.course_uuid == course_uuid, Course.org_id == token_user.org_id))).scalars().first()
    if not course:
        raise HTTPException(status_code=404, detail='Course not found')
    member_ids = set((await db_session.execute(select(UserOrganization.user_id).where(UserOrganization.user_id.in_(user_ids), UserOrganization.org_id == token_user.org_id))).scalars().all())
    already_enrolled_ids = set((await db_session.execute(select(TrailRun.user_id).where(TrailRun.course_id == course.id, TrailRun.user_id.in_(user_ids), TrailRun.org_id == token_user.org_id))).scalars().all())
    enrolled: List[int] = []
    already_enrolled: List[int] = []
    skipped: List[int] = []
    to_enroll: List[int] = []
    for user_id in user_ids:
        if user_id not in member_ids:
            skipped.append(user_id)
        elif user_id in already_enrolled_ids:
            already_enrolled.append(user_id)
        else:
            to_enroll.append(user_id)
    if not to_enroll:
        return {'enrolled': enrolled, 'already_enrolled': already_enrolled, 'skipped': skipped}
    now = datetime.now()
    trails_by_user: dict[int, Trail] = {t.user_id: t for t in (await db_session.execute(select(Trail).where(Trail.org_id == token_user.org_id, Trail.user_id.in_(to_enroll)))).scalars().all()}
    new_trails = []
    for user_id in to_enroll:
        if user_id not in trails_by_user:
            t = Trail(org_id=token_user.org_id, user_id=user_id, trail_uuid=f'trail_{uuid4()}', creation_date=str(now), update_date=str(now))
            new_trails.append(t)
            db_session.add(t)
    if new_trails:
        await db_session.flush()
        for t in new_trails:
            trails_by_user[t.user_id] = t
    for user_id in to_enroll:
        trail = trails_by_user[user_id]
        db_session.add(TrailRun(trail_id=trail.id if trail.id is not None else 0, course_id=course.id if course.id is not None else 0, org_id=token_user.org_id, user_id=user_id, creation_date=str(now), update_date=str(now)))
        enrolled.append(user_id)
    await db_session.commit()
    for user_id in enrolled:
        await track(event_name=analytics_events.COURSE_ENROLLED, org_id=token_user.org_id, user_id=user_id, properties={'course_uuid': course.course_uuid})
    return {'enrolled': enrolled, 'already_enrolled': already_enrolled, 'skipped': skipped}

async def _org_signup_field_keys(org_id: int, db_session: AsyncSession) -> set[str]:
    """Keys of the org's declared signup fields (what its admins may read)."""
    from src.services.orgs.signup_fields import get_org_signup_fields
    return {f.key for f in await get_org_signup_fields(org_id, db_session)}

async def list_course_enrollments(token_user: APITokenUser, course_uuid: str, db_session: AsyncSession, page: int=1, limit: int=25) -> List[dict]:
    """List users enrolled in a course within the token's org."""
    _require_token_right(token_user, 'courses', 'read')
    course = (await db_session.execute(select(Course).where(Course.course_uuid == course_uuid, Course.org_id == token_user.org_id))).scalars().first()
    if not course:
        raise HTTPException(status_code=404, detail='Course not found')
    offset = (page - 1) * limit
    rows = (await db_session.execute(select(User, TrailRun).join(TrailRun, TrailRun.user_id == User.id).where(TrailRun.course_id == course.id, TrailRun.org_id == token_user.org_id).order_by(TrailRun.creation_date.desc()).offset(offset).limit(limit))).all()
    meta_keys = await _org_signup_field_keys(token_user.org_id, db_session)
    return [{'user': OrgMemberUserRead.for_org(user, meta_keys).model_dump(), 'enrolled_at': trail_run.creation_date, 'status': trail_run.status.value if hasattr(trail_run.status, 'value') else str(trail_run.status)} for user, trail_run in rows]

async def reset_user_progress(token_user: APITokenUser, user_id: int, course_uuid: str, db_session: AsyncSession) -> dict:
    """Delete a user's trail steps for a course. Keeps TrailRun enrollment intact."""
    _require_token_right(token_user, 'courses', 'update')
    await _get_user_in_org(user_id, token_user.org_id, db_session)
    course = (await db_session.execute(select(Course).where(Course.course_uuid == course_uuid, Course.org_id == token_user.org_id))).scalars().first()
    if not course:
        raise HTTPException(status_code=404, detail='Course not found')
    steps = (await db_session.execute(select(TrailStep).where(TrailStep.user_id == user_id, TrailStep.course_id == course.id, TrailStep.org_id == token_user.org_id))).scalars().all()
    deleted = 0
    for step in steps:
        await db_session.delete(step)
        deleted += 1
    trail_run = (await db_session.execute(select(TrailRun).where(TrailRun.course_id == course.id, TrailRun.user_id == user_id, TrailRun.org_id == token_user.org_id))).scalars().first()
    if trail_run:
        from src.db.trail_runs import StatusEnum
        trail_run.status = StatusEnum.STATUS_IN_PROGRESS
        trail_run.update_date = str(datetime.now())
        db_session.add(trail_run)
    await db_session.commit()
    notify_ee_learner_progress(user_id, course.id)
    return {'course_uuid': course_uuid, 'user_id': user_id, 'steps_deleted': deleted}

async def award_certificate(token_user: APITokenUser, user_id: int, course_uuid: str, request: Request, db_session: AsyncSession) -> dict:
    """Manually award a certificate, bypassing the completion gate."""
    _require_token_right(token_user, 'certifications', 'create')
    await _get_user_in_org(user_id, token_user.org_id, db_session)
    course = (await db_session.execute(select(Course).where(Course.course_uuid == course_uuid, Course.org_id == token_user.org_id))).scalars().first()
    if not course:
        raise HTTPException(status_code=404, detail='Course not found')
    certification = (await db_session.execute(select(Certifications).where(Certifications.course_id == course.id))).scalars().first()
    if not certification:
        raise HTTPException(status_code=404, detail='Course has no certification configured')
    cert_user = await create_certificate_user(request=request, user_id=user_id, certification_id=certification.id, db_session=db_session, current_user=None)
    return {'user_certification_uuid': cert_user.user_certification_uuid, 'user_id': user_id, 'course_uuid': course_uuid}

async def revoke_certificate(token_user: APITokenUser, user_id: int, user_certification_uuid: str, db_session: AsyncSession) -> dict:
    """Delete a user's certificate. Verifies cross-org boundary."""
    _require_token_right(token_user, 'certifications', 'delete')
    await _get_user_in_org(user_id, token_user.org_id, db_session)
    cert_user = (await db_session.execute(select(CertificateUser).where(CertificateUser.user_certification_uuid == user_certification_uuid, CertificateUser.user_id == user_id))).scalars().first()
    if not cert_user:
        raise HTTPException(status_code=404, detail='Certificate not found')
    certification = (await db_session.execute(select(Certifications).where(Certifications.id == cert_user.certification_id))).scalars().first()
    if not certification:
        raise HTTPException(status_code=404, detail='Certificate not found')
    course = (await db_session.execute(select(Course).where(Course.id == certification.course_id))).scalars().first()
    if not course or course.org_id != token_user.org_id:
        raise HTTPException(status_code=404, detail='Certificate not found')
    await db_session.delete(cert_user)
    await db_session.commit()
    await track(event_name=analytics_events.CERTIFICATE_REVOKED, org_id=token_user.org_id, user_id=user_id, properties={'course_uuid': course.course_uuid})
    await dispatch_webhooks(event_name=analytics_events.CERTIFICATE_REVOKED, org_id=token_user.org_id, data={'user_id': user_id, 'user_certification_uuid': user_certification_uuid, 'course_uuid': course.course_uuid})
    return {'detail': 'Certificate revoked', 'user_certification_uuid': user_certification_uuid}

async def _get_usergroup_in_org(usergroup_uuid: str, org_id: int, db_session: AsyncSession) -> UserGroup:
    group = (await db_session.execute(select(UserGroup).where(UserGroup.usergroup_uuid == usergroup_uuid, UserGroup.org_id == org_id))).scalars().first()
    if not group:
        raise HTTPException(status_code=404, detail='UserGroup not found')
    return group

async def add_usergroup_member(token_user: APITokenUser, usergroup_uuid: str, user_id: int, db_session: AsyncSession) -> dict:
    """Add a user to a user group. User and group must both be in the token's org."""
    _require_token_right(token_user, 'usergroups', 'update')
    await _get_user_in_org(user_id, token_user.org_id, db_session)
    group = await _get_usergroup_in_org(usergroup_uuid, token_user.org_id, db_session)
    existing = (await db_session.execute(select(UserGroupUser).where(UserGroupUser.usergroup_id == group.id, UserGroupUser.user_id == user_id))).scalars().first()
    if existing:
        raise HTTPException(status_code=400, detail='User is already in this group')
    now = datetime.now()
    member = UserGroupUser(usergroup_id=group.id if group.id is not None else 0, user_id=user_id, org_id=token_user.org_id, creation_date=str(now), update_date=str(now))
    db_session.add(member)
    await db_session.commit()
    await dispatch_webhooks(event_name='usergroup_users_added', org_id=token_user.org_id, data={'usergroup_id': group.id, 'usergroup_uuid': group.usergroup_uuid, 'user_ids': [user_id]})
    return {'detail': 'User added to group', 'usergroup_uuid': usergroup_uuid, 'user_id': user_id}

async def remove_usergroup_member(token_user: APITokenUser, usergroup_uuid: str, user_id: int, db_session: AsyncSession) -> dict:
    """Remove a user from a user group."""
    _require_token_right(token_user, 'usergroups', 'update')
    await _get_user_in_org(user_id, token_user.org_id, db_session)
    group = await _get_usergroup_in_org(usergroup_uuid, token_user.org_id, db_session)
    member = (await db_session.execute(select(UserGroupUser).where(UserGroupUser.usergroup_id == group.id, UserGroupUser.user_id == user_id))).scalars().first()
    if not member:
        raise HTTPException(status_code=404, detail='User is not a member of this group')
    await db_session.delete(member)
    await db_session.commit()
    await dispatch_webhooks(event_name='usergroup_users_removed', org_id=token_user.org_id, data={'usergroup_id': group.id, 'usergroup_uuid': group.usergroup_uuid, 'user_ids': [user_id]})
    return {'detail': 'User removed from group', 'usergroup_uuid': usergroup_uuid, 'user_id': user_id}

async def get_user_certificates(token_user: APITokenUser, user_id: int, db_session: AsyncSession) -> List[dict]:
    """Get all certificates for a user in the token's org."""
    _require_token_right(token_user, 'certifications', 'read')
    await _get_user_in_org(user_id, token_user.org_id, db_session)
    cert_users = (await db_session.execute(select(CertificateUser).where(CertificateUser.user_id == user_id))).scalars().all()
    if not cert_users:
        return []
    cert_ids = list({cu.certification_id for cu in cert_users})
    certifications = (await db_session.execute(select(Certifications).where(Certifications.id.in_(cert_ids)))).scalars().all()
    cert_map = {c.id: c for c in certifications}
    course_ids = list({c.course_id for c in certifications if c.course_id})
    courses = (await db_session.execute(select(Course).where(Course.id.in_(course_ids)))).scalars().all() if course_ids else []
    course_map = {c.id: c for c in courses}
    result = []
    for cu in cert_users:
        cert = cert_map.get(cu.certification_id)
        if not cert:
            continue
        course = course_map.get(cert.course_id)
        if not course or course.org_id != token_user.org_id:
            continue
        result.append({'certificate_user': CertificateUserRead(**cu.model_dump()).model_dump(), 'certification': CertificationRead(**cert.model_dump()).model_dump(), 'course': {'id': course.id, 'course_uuid': course.course_uuid, 'name': course.name, 'description': course.description, 'thumbnail_image': course.thumbnail_image}})
    return result
_USER_UPDATABLE_FIELDS = {'username', 'first_name', 'last_name', 'email', 'avatar_image'}

async def update_user_profile(token_user: APITokenUser, user_id: int, updates: dict, db_session: AsyncSession) -> OrgMemberUserRead:
    """Update a user's profile fields. Org-scoped: user must be a member."""
    _require_token_right(token_user, 'users', 'update')
    user = await _get_user_in_org(user_id, token_user.org_id, db_session)
    await _check_token_can_edit_account(user, token_user.org_id, db_session)
    if 'email' in updates and updates['email'] != user.email:
        existing = (await db_session.execute(select(User).where(User.email == updates['email'], User.id != user_id))).scalars().first()
        if existing:
            raise HTTPException(status_code=400, detail='Email already in use')
    if 'username' in updates and updates['username'] != user.username:
        existing = (await db_session.execute(select(User).where(User.username == updates['username'], User.id != user_id))).scalars().first()
        if existing:
            raise HTTPException(status_code=400, detail='Username already in use')
    name_check = validate_profile_fields({'username': updates.get('username'), 'first_name': updates.get('first_name'), 'last_name': updates.get('last_name')})
    if not name_check.is_valid:
        raise HTTPException(status_code=400, detail={'code': 'PROFILE_FIELD_INVALID', 'message': 'Display name fields may not contain URLs or links', 'errors': name_check.errors, 'invalid_fields': name_check.invalid_fields})
    new_email = updates.get('email')
    if new_email is not None and new_email != user.email:
        user.email_verified = False
        user.email_verified_at = None
    for field, value in updates.items():
        if field in _USER_UPDATABLE_FIELDS and value is not None:
            setattr(user, field, value)
    user.update_date = str(datetime.now())
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    try:
        from src.routers.users import _invalidate_session_cache
        _invalidate_session_cache(user_id)
    except Exception:
        pass
    meta_keys = await _org_signup_field_keys(token_user.org_id, db_session)
    return OrgMemberUserRead.for_org(user, meta_keys)

async def change_user_role(token_user: APITokenUser, user_id: int, new_role_id: int, db_session: AsyncSession) -> dict:
    """Change a user's org role. Blocks demoting the last admin."""
    _require_token_right(token_user, 'users', 'update')
    _require_token_right(token_user, 'roles', 'update')
    await _get_user_in_org(user_id, token_user.org_id, db_session)
    await _check_token_can_manage_membership(user_id, token_user.org_id, db_session)
    role = (await db_session.execute(select(Role).where(Role.id == new_role_id))).scalars().first()
    if not role:
        raise HTTPException(status_code=404, detail='Role not found')
    if role.org_id is not None and role.org_id != token_user.org_id:
        raise HTTPException(status_code=403, detail='Role does not belong to this organization')
    membership = (await db_session.execute(select(UserOrganization).where(UserOrganization.user_id == user_id, UserOrganization.org_id == token_user.org_id))).scalars().first()
    if not membership:
        raise HTTPException(status_code=404, detail='User not in org')
    await _check_token_can_assign_role(token_user, role, db_session)
    if membership.role_id == ADMIN_ROLE_ID and new_role_id != ADMIN_ROLE_ID:
        admin_count = len((await db_session.execute(select(UserOrganization).where(UserOrganization.org_id == token_user.org_id, UserOrganization.role_id == ADMIN_ROLE_ID))).scalars().all())
        if admin_count <= 1:
            raise HTTPException(status_code=400, detail='Cannot demote the last admin of the organization')
    await enforce_admin_seat_limit_for_role_change(token_user.org_id, user_id, role, db_session)
    membership.role_id = new_role_id
    membership.update_date = str(datetime.now())
    db_session.add(membership)
    await db_session.commit()
    try:
        from src.routers.users import _invalidate_session_cache
        _invalidate_session_cache(user_id)
    except Exception:
        pass
    return {'user_id': user_id, 'role_id': new_role_id}

async def create_usergroup(token_user: APITokenUser, name: str, description: str, db_session: AsyncSession) -> UserGroupRead:
    """Create a user group / cohort in the token's org."""
    _require_token_right(token_user, 'usergroups', 'create')
    await check_limits_with_usage('usergroups', token_user.org_id, db_session)
    now = datetime.now()
    group = UserGroup(name=name, description=description, org_id=token_user.org_id, usergroup_uuid=f'usergroup_{uuid4()}', creation_date=str(now), update_date=str(now))
    db_session.add(group)
    await db_session.commit()
    await db_session.refresh(group)
    await dispatch_webhooks(event_name='usergroup_created', org_id=token_user.org_id, data={'usergroup_uuid': group.usergroup_uuid, 'name': group.name})
    return UserGroupRead.model_validate(group)

async def delete_usergroup(token_user: APITokenUser, usergroup_uuid: str, db_session: AsyncSession) -> dict:
    """Delete a user group and all its memberships + resource links."""
    _require_token_right(token_user, 'usergroups', 'delete')
    group = await _get_usergroup_in_org(usergroup_uuid, token_user.org_id, db_session)
    members = (await db_session.execute(select(UserGroupUser).where(UserGroupUser.usergroup_id == group.id))).scalars().all()
    for m in members:
        await db_session.delete(m)
    resources = (await db_session.execute(select(UserGroupResource).where(UserGroupResource.usergroup_id == group.id))).scalars().all()
    for r in resources:
        await db_session.delete(r)
    await db_session.delete(group)
    await db_session.commit()
    await dispatch_webhooks(event_name='usergroup_deleted', org_id=token_user.org_id, data={'usergroup_uuid': usergroup_uuid})
    return {'detail': 'UserGroup deleted', 'usergroup_uuid': usergroup_uuid}

async def list_usergroup_members(token_user: APITokenUser, usergroup_uuid: str, db_session: AsyncSession, page: int=1, limit: int=25) -> List[dict]:
    """List users in a cohort, with pagination."""
    _require_token_right(token_user, 'usergroups', 'read')
    group = await _get_usergroup_in_org(usergroup_uuid, token_user.org_id, db_session)
    offset = (page - 1) * limit
    rows = (await db_session.execute(select(User, UserGroupUser).join(UserGroupUser, UserGroupUser.user_id == User.id).where(UserGroupUser.usergroup_id == group.id).order_by(UserGroupUser.creation_date.asc()).offset(offset).limit(limit))).all()
    meta_keys = await _org_signup_field_keys(token_user.org_id, db_session)
    return [{'user': OrgMemberUserRead.for_org(user, meta_keys).model_dump(), 'added_at': membership.creation_date} for user, membership in rows]

async def get_user_groups(token_user: APITokenUser, user_id: int, db_session: AsyncSession) -> List[dict]:
    """List user groups a user belongs to within the token's org."""
    _require_token_right(token_user, 'usergroups', 'read')
    await _get_user_in_org(user_id, token_user.org_id, db_session)
    rows = (await db_session.execute(select(UserGroup, UserGroupUser).join(UserGroupUser, UserGroupUser.usergroup_id == UserGroup.id).where(UserGroupUser.user_id == user_id, UserGroup.org_id == token_user.org_id).order_by(UserGroupUser.creation_date.asc()))).all()
    return [{'usergroup': UserGroupRead.model_validate(group).model_dump(), 'added_at': membership.creation_date} for group, membership in rows]

async def add_course_to_usergroup(token_user: APITokenUser, usergroup_uuid: str, course_uuid: str, db_session: AsyncSession) -> dict:
    """Grant a cohort access to a course."""
    _require_token_right(token_user, 'usergroups', 'update')
    group = await _get_usergroup_in_org(usergroup_uuid, token_user.org_id, db_session)
    course = (await db_session.execute(select(Course).where(Course.course_uuid == course_uuid, Course.org_id == token_user.org_id))).scalars().first()
    if not course:
        raise HTTPException(status_code=404, detail='Course not found')
    existing = (await db_session.execute(select(UserGroupResource).where(UserGroupResource.usergroup_id == group.id, UserGroupResource.resource_uuid == course.course_uuid))).scalars().first()
    if existing:
        raise HTTPException(status_code=400, detail='Course is already linked to this group')
    now = datetime.now()
    link = UserGroupResource(usergroup_id=group.id if group.id is not None else 0, resource_uuid=course.course_uuid, org_id=token_user.org_id, creation_date=str(now), update_date=str(now))
    db_session.add(link)
    await db_session.commit()
    await dispatch_webhooks(event_name='usergroup_resources_added', org_id=token_user.org_id, data={'usergroup_uuid': group.usergroup_uuid, 'resource_uuids': [course.course_uuid]})
    return {'detail': 'Course linked to group', 'usergroup_uuid': usergroup_uuid, 'course_uuid': course_uuid}

async def remove_course_from_usergroup(token_user: APITokenUser, usergroup_uuid: str, course_uuid: str, db_session: AsyncSession) -> dict:
    """Revoke a cohort's access to a course."""
    _require_token_right(token_user, 'usergroups', 'update')
    group = await _get_usergroup_in_org(usergroup_uuid, token_user.org_id, db_session)
    link = (await db_session.execute(select(UserGroupResource).where(UserGroupResource.usergroup_id == group.id, UserGroupResource.resource_uuid == course_uuid))).scalars().first()
    if not link:
        raise HTTPException(status_code=404, detail='Course not linked to this group')
    await db_session.delete(link)
    await db_session.commit()
    await dispatch_webhooks(event_name='usergroup_resources_removed', org_id=token_user.org_id, data={'usergroup_uuid': group.usergroup_uuid, 'resource_uuids': [course_uuid]})
    return {'detail': 'Course unlinked from group', 'usergroup_uuid': usergroup_uuid, 'course_uuid': course_uuid}

async def bulk_unenroll_users(token_user: APITokenUser, course_uuid: str, user_ids: List[int], db_session: AsyncSession) -> dict:
    """Unenroll a batch of users from a course. Returns summary."""
    _require_token_right(token_user, 'courses', 'update')
    from sqlmodel import delete as sql_delete
    course = (await db_session.execute(select(Course).where(Course.course_uuid == course_uuid, Course.org_id == token_user.org_id))).scalars().first()
    if not course:
        raise HTTPException(status_code=404, detail='Course not found')
    enrolled_ids = set((await db_session.execute(select(TrailRun.user_id).where(TrailRun.course_id == course.id, TrailRun.user_id.in_(user_ids), TrailRun.org_id == token_user.org_id))).scalars().all())
    unenrolled = [uid for uid in user_ids if uid in enrolled_ids]
    not_enrolled = [uid for uid in user_ids if uid not in enrolled_ids]
    if unenrolled:
        await db_session.execute(sql_delete(TrailStep).where(TrailStep.course_id == course.id, TrailStep.user_id.in_(unenrolled), TrailStep.org_id == token_user.org_id))
        await db_session.execute(sql_delete(TrailRun).where(TrailRun.course_id == course.id, TrailRun.user_id.in_(unenrolled), TrailRun.org_id == token_user.org_id))
        await db_session.commit()
        for uid in unenrolled:
            notify_ee_learner_progress(uid, course.id)
            await track(event_name=analytics_events.COURSE_LEFT, org_id=token_user.org_id, user_id=uid, properties={'course_uuid': course.course_uuid})
    return {'unenrolled': unenrolled, 'not_enrolled': not_enrolled}

async def export_user_data(token_user: APITokenUser, user_id: int, db_session: AsyncSession) -> dict:
    """Full GDPR data export scoped to the token's org.

    Only returns data that belongs to the token's organization. Other-org
    memberships and certificates are intentionally excluded so a token for
    org A cannot read a user's history in org B.
    """
    _require_token_right(token_user, 'users', 'read')
    user = await _get_user_in_org(user_id, token_user.org_id, db_session)
    memberships = (await db_session.execute(select(UserOrganization).where(UserOrganization.user_id == user_id, UserOrganization.org_id == token_user.org_id))).scalars().all()
    trails = (await db_session.execute(select(Trail).where(Trail.user_id == user_id, Trail.org_id == token_user.org_id))).scalars().all()
    trail_runs = (await db_session.execute(select(TrailRun).where(TrailRun.user_id == user_id, TrailRun.org_id == token_user.org_id))).scalars().all()
    trail_steps = (await db_session.execute(select(TrailStep).where(TrailStep.user_id == user_id, TrailStep.org_id == token_user.org_id))).scalars().all()
    cert_rows = (await db_session.execute(select(CertificateUser, Certifications, Course).join(Certifications, Certifications.id == CertificateUser.certification_id).join(Course, Course.id == Certifications.course_id).where(CertificateUser.user_id == user_id, Course.org_id == token_user.org_id))).all()
    cert_users = [cu for cu, _cert, _course in cert_rows]
    group_rows = (await db_session.execute(select(UserGroup, UserGroupUser).join(UserGroupUser, UserGroupUser.usergroup_id == UserGroup.id).where(UserGroupUser.user_id == user_id, UserGroup.org_id == token_user.org_id))).all()
    return {'profile': OrgMemberUserRead.for_org(user, await _org_signup_field_keys(token_user.org_id, db_session)).model_dump(), 'memberships': [m.model_dump() for m in memberships], 'trails': [t.model_dump() for t in trails], 'trail_runs': [tr.model_dump() for tr in trail_runs], 'trail_steps': [ts.model_dump() for ts in trail_steps], 'certificates': [CertificateUserRead(**cu.model_dump()).model_dump() for cu in cert_users], 'user_groups': [UserGroupRead.model_validate(g).model_dump() for g, _ in group_rows], 'exported_at': datetime.now().isoformat()}


async def anonymize_user(token_user: APITokenUser, user_id: int, db_session: AsyncSession) -> dict:
    """GDPR right-to-be-forgotten. Scrub PII, delete API tokens, invalidate session.

    Cross-org note: the ``User`` row is global, so scrubbing it would affect
    every org the user belongs to. Accounts that also belong to another org
    (or are privileged) are refused; erasing those needs platform authority.
    """
    _require_token_right(token_user, 'users', 'delete')
    user = await _get_user_in_org(user_id, token_user.org_id, db_session)
    await _check_token_can_edit_account(user, token_user.org_id, db_session)
    placeholder_email = f'deleted-user-{user_id}@anonymized.example.com'
    placeholder_username = f'deleted_user_{user_id}'
    existing_tokens = (await db_session.execute(select(APIToken).where(APIToken.created_by_user_id == user_id, APIToken.org_id == token_user.org_id))).scalars().all()
    for token in existing_tokens:
        await db_session.delete(token)
    user.email = placeholder_email
    user.username = placeholder_username
    user.first_name = 'Deleted'
    user.last_name = 'User'
    user.avatar_image = ''
    user.password = ''
    user.email_verified = False
    user.email_verified_at = None
    user.extra_metadata = {}
    user.last_login_at = None
    user.last_login_ip = None
    user.failed_login_attempts = 0
    user.locked_until = None
    user.signup_method = 'anonymized'
    user.update_date = str(datetime.now())
    db_session.add(user)
    from sqlalchemy import update as sa_update
    from src.db.user_audit_events import UserAuditEvent
    await db_session.execute(sa_update(UserAuditEvent).where(UserAuditEvent.user_id == user_id).values(ip=None, user_agent=None))
    await db_session.commit()
    try:
        from src.routers.users import _invalidate_session_cache
        _invalidate_session_cache(user_id)
    except Exception:
        pass
    await dispatch_webhooks(event_name='user_anonymized', org_id=token_user.org_id, data={'user_id': user_id, 'anonymized_email': placeholder_email, 'api_tokens_revoked': len(existing_tokens)})
    return {'detail': 'User anonymized', 'user_id': user_id, 'anonymized_email': placeholder_email, 'api_tokens_revoked': len(existing_tokens)}

async def get_course_analytics(token_user: APITokenUser, course_uuid: str, db_session: AsyncSession) -> dict:
    """Aggregate course stats: enrollment, completion, in-progress, cert count."""
    _require_token_right(token_user, 'courses', 'read')
    from src.db.trail_runs import StatusEnum
    course = (await db_session.execute(select(Course).where(Course.course_uuid == course_uuid, Course.org_id == token_user.org_id))).scalars().first()
    if not course:
        raise HTTPException(status_code=404, detail='Course not found')
    total_activities = (await db_session.execute(select(func.count(ChapterActivity.id)).where(ChapterActivity.course_id == course.id))).scalar_one()
    trail_runs = (await db_session.execute(select(TrailRun).where(TrailRun.course_id == course.id, TrailRun.org_id == token_user.org_id))).scalars().all()
    enrollment_count = len(trail_runs)
    completed_count = sum((1 for tr in trail_runs if tr.status == StatusEnum.STATUS_COMPLETED))
    in_progress_count = sum((1 for tr in trail_runs if tr.status == StatusEnum.STATUS_IN_PROGRESS))
    average_completion_percentage = 0.0
    if trail_runs and total_activities:
        completion_rows = (await db_session.execute(select(TrailStep.user_id, func.count(TrailStep.id)).where(TrailStep.course_id == course.id, TrailStep.complete == True).group_by(TrailStep.user_id))).all()
        completed_by_user = {row[0]: row[1] for row in completion_rows}
        if trail_runs:
            total_pct = sum((completed_by_user.get(tr.user_id, 0) / total_activities * 100 for tr in trail_runs))
            average_completion_percentage = round(total_pct / len(trail_runs), 1)
    certificate_count = 0
    certification = (await db_session.execute(select(Certifications).where(Certifications.course_id == course.id))).scalars().first()
    if certification:
        certificate_count = (await db_session.execute(select(func.count(CertificateUser.id)).where(CertificateUser.certification_id == certification.id))).scalar_one()
    return {'course_uuid': course_uuid, 'enrollment_count': enrollment_count, 'completed_count': completed_count, 'in_progress_count': in_progress_count, 'total_activities': total_activities, 'average_completion_percentage': average_completion_percentage, 'certificate_count': certificate_count}
