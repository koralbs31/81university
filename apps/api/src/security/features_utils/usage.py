from src.db.organization_config import OrganizationConfig
from src.db.user_organizations import UserOrganization
from src.db.courses.courses import Course
from src.db.roles import Role, RoleTypeEnum
from src.db.usergroups import UserGroup
from src.db.courses.assignments import Assignment
from sqlalchemy import or_
from src.core.deployment_mode import get_deployment_mode
from src.core.redis import get_redis_client as _get_redis_pool_client
from typing import Literal, Optional, TypeAlias
from fastapi import HTTPException
from sqlmodel import select, func
from sqlmodel.ext.asyncio.session import AsyncSession
from src.security.features_utils.plans import PlanLevel, get_plan_limit, plan_meets_requirement, get_required_plan_for_feature
FeatureSet: TypeAlias = Literal['admin_seats', 'analytics', 'api', 'assignments', 'courses', 'members', 'usergroups']
PLAN_BASED_FEATURES = {'courses', 'members', 'admin_seats'}
REDIS_TRACKED_FEATURES = {'analytics', 'api', 'assignments', 'usergroups'}
DB_COUNTED_FEATURES = PLAN_BASED_FEATURES | {'usergroups', 'assignments'}

def _is_non_saas() -> bool:
    """Check if deployment is in a non-SaaS mode (EE or OSS), which disables plan-based limits."""
    return get_deployment_mode() != 'saas'

def _get_redis_client():
    """Get a Redis client from the shared pool. Raises HTTP 500 if unavailable."""
    r = _get_redis_pool_client()
    if r is None:
        raise HTTPException(status_code=500, detail='Redis connection string not found')
    return r

def _plan_from_config_dict(config: dict) -> PlanLevel:
    """Read the plan out of a raw org config dict (supports v1 and v2 layout)."""
    config = config or {}
    version = str(config.get('config_version', '1.0'))
    if version.startswith('2'):
        return config.get('plan', 'free')
    return config.get('cloud', {}).get('plan', 'free')

def _get_org_plan(org_config: OrganizationConfig) -> PlanLevel:
    """Get the organization's current plan (supports v1 and v2 config)."""
    return _plan_from_config_dict(org_config.config or {})

async def _get_actual_member_count(org_id: int, db_session: AsyncSession) -> int:
    """Get actual member count from database."""
    statement = select(func.count()).where(UserOrganization.org_id == org_id)
    return (await db_session.execute(statement)).scalar_one()

async def _get_actual_course_count(org_id: int, db_session: AsyncSession) -> int:
    """Get actual course count from database."""
    statement = select(func.count()).where(Course.org_id == org_id)
    return (await db_session.execute(statement)).scalar_one()

async def _get_actual_admin_seat_count(org_id: int, db_session: AsyncSession) -> int:
    """
    Get count of users with dashboard access (admin seats).
    Admin seat = user with a role that has dashboard.action_access = true
    """
    statement = select(Role).where(or_(Role.org_id == org_id, Role.role_type == RoleTypeEnum.TYPE_GLOBAL))
    roles = (await db_session.execute(statement)).scalars().all()
    admin_role_ids = []
    for role in roles:
        rights = role.rights
        if isinstance(rights, dict):
            dashboard = rights.get('dashboard', {})
            if dashboard.get('action_access', False):
                admin_role_ids.append(role.id)
    if not admin_role_ids:
        return 0
    statement = select(func.count()).where(UserOrganization.org_id == org_id, UserOrganization.role_id.in_(admin_role_ids))
    return (await db_session.execute(statement)).scalar_one()

async def _get_actual_usergroup_count(org_id: int, db_session: AsyncSession) -> int:
    """Get actual usergroup count from the database."""
    statement = select(func.count()).where(UserGroup.org_id == org_id)
    return (await db_session.execute(statement)).scalar_one()

async def _get_actual_assignment_count(org_id: int, db_session: AsyncSession) -> int:
    """Get actual assignment count from the database."""
    statement = select(func.count()).where(Assignment.org_id == org_id)
    return (await db_session.execute(statement)).scalar_one()

async def _get_actual_usage(feature: str, org_id: int, db_session: AsyncSession) -> int:
    """Get actual usage count from the database for DB-counted features."""
    if feature == 'members':
        return await _get_actual_member_count(org_id, db_session)
    elif feature == 'courses':
        return await _get_actual_course_count(org_id, db_session)
    elif feature == 'admin_seats':
        return await _get_actual_admin_seat_count(org_id, db_session)
    elif feature == 'usergroups':
        return await _get_actual_usergroup_count(org_id, db_session)
    elif feature == 'assignments':
        return await _get_actual_assignment_count(org_id, db_session)
    return 0

def _invalidate_usage_cache(org_id: int) -> None:
    """Invalidate the usage cache for an organization."""
    try:
        r = _get_redis_client()
        r.delete(f'org_usage:{org_id}')
    except Exception:
        pass

async def _get_org_config(org_id: int, db_session: AsyncSession) -> Optional[OrganizationConfig]:
    """Return OrganizationConfig with a Redis read-aside cache."""
    from src.services.orgs.cache import get_cached_org_config, set_cached_org_config
    raw = get_cached_org_config(org_id)
    if raw is not None:
        try:
            return OrganizationConfig(**raw)
        except Exception:
            pass
    stmt = select(OrganizationConfig).where(OrganizationConfig.org_id == org_id)
    org_config = (await db_session.execute(stmt)).scalar_one_or_none()
    if org_config is not None:
        try:
            set_cached_org_config(org_id, org_config.model_dump(mode='json'))
        except Exception:
            pass
    return org_config

async def check_feature_enabled(feature: FeatureSet, org_id: int, db_session: AsyncSession) -> bool:
    """
    Check if a feature is enabled for an organization.

    Uses resolve_feature() for v2 configs, falls back to plan-based check for v1.

    Returns:
        True if the feature is enabled

    Raises:
        HTTPException 403 if feature is disabled
    """
    from src.security.features_utils.resolve import resolve_feature
    org_config = await _get_org_config(org_id, db_session)
    if org_config is None:
        raise HTTPException(status_code=404, detail='Organization has no config')
    resolved = resolve_feature(feature, org_config.config or {}, org_id)
    if not resolved['enabled']:
        raise HTTPException(status_code=403, detail=f'{feature.capitalize()} is not enabled for this organization')
    return True

async def check_limits_with_usage(feature: FeatureSet, org_id: int, db_session: AsyncSession):
    """Check if usage is within limits for a feature.
    Uses resolve_feature() for unified 4-layer resolution."""
    from src.security.features_utils.resolve import resolve_feature
    org_config = await _get_org_config(org_id, db_session)
    if org_config is None:
        raise HTTPException(status_code=404, detail='Organization has no config')
    resolved = resolve_feature(feature, org_config.config or {}, org_id)
    if not resolved['enabled']:
        raise HTTPException(status_code=403, detail=f'{feature.capitalize()} is not enabled for this organization')
    if resolved['limit'] == 0:
        return True
    org_plan = _get_org_plan(org_config)
    feature_limit = resolved['limit']
    if feature in DB_COUNTED_FEATURES:
        current_usage = await _get_actual_usage(feature, org_id, db_session)
        if current_usage >= feature_limit:
            if org_plan not in ('free',):
                return True
            else:
                raise HTTPException(status_code=403, detail=f'Usage Limit has been reached for {feature.capitalize()}')
        return True
    if feature_limit > 0:
        r = _get_redis_client()
        feature_usage = r.get(f'{feature}_usage:{org_id}')
        if feature_usage is None:
            feature_usage_count = 0
        else:
            feature_usage_count = int(feature_usage)
        if feature_limit <= feature_usage_count:
            raise HTTPException(status_code=403, detail=f'Usage Limit has been reached for {feature.capitalize()}')
    return True

async def check_members_limit_with_pending(org_id: int, pending_and_new: int, db_session: AsyncSession):
    """Enforce the members limit counting BOTH joined members and pending/new
    invites.

    ``check_limits_with_usage("members", ...)`` only counts users who have
    already joined, so a free org at (or near) its cap could still queue an
    unbounded number of pending invitations. This folds ``pending_and_new``
    (existing pending invites + the new ones about to be sent) into the count.

    No-op when the members feature is unlimited (limit 0) or the org is on a
    paid plan (paid plans allow tracked overage).
    """
    from src.security.features_utils.resolve import resolve_feature
    org_config = await _get_org_config(org_id, db_session)
    if org_config is None:
        raise HTTPException(status_code=404, detail='Organization has no config')
    resolved = resolve_feature('members', org_config.config or {}, org_id)
    if not resolved['enabled']:
        raise HTTPException(status_code=403, detail='Members is not enabled for this organization')
    feature_limit = resolved['limit']
    if feature_limit == 0:
        return True
    if _get_org_plan(org_config) not in ('free',):
        return True
    current = await _get_actual_member_count(org_id, db_session)
    if current + max(pending_and_new, 0) > feature_limit:
        raise HTTPException(status_code=403, detail='Usage Limit has been reached for Members')
    return True

async def increase_feature_usage(feature: FeatureSet, org_id: int, db_session: AsyncSession):
    """Increase usage count for a feature."""
    if feature in PLAN_BASED_FEATURES:
        _invalidate_usage_cache(org_id)
        return True
    r = _get_redis_client()
    feature_usage = r.get(f'{feature}_usage:{org_id}')
    if feature_usage is None:
        feature_usage_count = 0
    else:
        feature_usage_count = int(feature_usage)
    r.set(f'{feature}_usage:{org_id}', feature_usage_count + 1)
    return True

async def decrease_feature_usage(feature: FeatureSet, org_id: int, db_session: AsyncSession):
    """Decrease usage count for a feature."""
    if feature in PLAN_BASED_FEATURES:
        _invalidate_usage_cache(org_id)
        return True
    r = _get_redis_client()
    feature_usage = r.get(f'{feature}_usage:{org_id}')
    if feature_usage is None:
        feature_usage_count = 0
    else:
        feature_usage_count = int(feature_usage)
    r.set(f'{feature}_usage:{org_id}', max(0, feature_usage_count - 1))
    return True

async def check_feature_access(feature: str, org_id: int, db_session: AsyncSession) -> bool:
    """
    Check if a feature is accessible based on plan level or OSS mode.

    For features that require a minimum plan level (e.g., versioning requires 'standard'),
    this function checks:
    1. If OSS mode is enabled → allow access
    2. If the organization's plan meets the required level → allow access
    3. Otherwise → deny access with 403

    Args:
        feature: The feature key (e.g., 'versioning', 'ai')
        org_id: The organization ID
        db_session: Database session

    Returns:
        True if access is allowed

    Raises:
        HTTPException 403 if access is denied
    """
    if _is_non_saas():
        return True
    required_plan = get_required_plan_for_feature(feature)
    if required_plan is None:
        return True
    org_config = await _get_org_config(org_id, db_session)
    if org_config is None:
        raise HTTPException(status_code=404, detail='Organization has no config')
    org_plan = _get_org_plan(org_config)
    if not plan_meets_requirement(org_plan, required_plan):
        raise HTTPException(status_code=403, detail=f'{feature.capitalize()} requires {required_plan} plan or higher. Current plan: {org_plan}')
    return True

async def check_admin_seat_limit(org_id: int, db_session: AsyncSession) -> bool:
    """
    Check if the organization can add another admin seat.
    Call this before assigning a role with dashboard access.

    Returns:
        True if allowed

    Raises:
        HTTPException 403 if the plan's admin-seat limit is already reached.

    NOTE: ``admin_seats`` is NOT a resolvable feature; it has no plan
    ``enabled`` flag of its own (it is derived from ``members.admin_limit``).
    Routing it through ``check_limits_with_usage``/``resolve_feature`` therefore
    resolved it as "disabled" for EVERY SaaS org and 403'd every admin promotion
    with "Admin_seats is not enabled". Resolve the limit directly from the plan
    (``get_plan_limit``) and enforce against the live seat count instead.
    """
    if _is_non_saas():
        return True
    org_config = await _get_org_config(org_id, db_session)
    if org_config is None:
        return True
    org_plan = _get_org_plan(org_config)
    if org_plan not in ('free',):
        return True
    limit = get_plan_limit(org_plan, 'admin_seats')
    if limit <= 0:
        return True
    current = await _get_actual_admin_seat_count(org_id, db_session)
    if current >= limit:
        raise HTTPException(status_code=403, detail='Usage Limit has been reached for Admin_seats')
    return True

def _role_grants_dashboard_access(role: Optional[Role]) -> bool:
    """True if a role occupies an admin seat (dashboard.action_access == true).

    Mirrors the seat-counting logic in ``_get_actual_admin_seat_count`` so the
    gate and the count agree on what an "admin seat" is. Tolerates ``rights``
    being either a plain dict or a Pydantic model.
    """
    if role is None:
        return False
    rights = role.rights
    if rights is None:
        return False
    if not isinstance(rights, dict):
        try:
            rights = rights.model_dump()
        except Exception:
            return False
    return bool(rights.get('dashboard', {}).get('action_access', False))

async def enforce_admin_seat_limit_for_role_rights_change(org_id: int, role_id: int, will_grant_dashboard: bool, currently_grants_dashboard: bool, db_session: AsyncSession) -> None:
    """Enforce the admin-seat cap when a role's ``dashboard.action_access`` is
    turned ON (false -> true).

    Because seats are derived from role rights, flipping a role that N members
    already hold instantly converts all N into admin seats, a bulk grant that
    the per-assignment gate cannot see. This validates the projected seat total
    (current seats + the holders about to be converted) against the plan limit.
    No-op for non-SaaS, paid plans, unlimited seats, or a role held by nobody.
    """
    if _is_non_saas():
        return
    if not will_grant_dashboard or currently_grants_dashboard:
        return
    holders = (await db_session.execute(select(func.count()).where(UserOrganization.org_id == org_id, UserOrganization.role_id == role_id))).scalar_one()
    if holders == 0:
        return
    org_config = await _get_org_config(org_id, db_session)
    if org_config is None:
        return
    org_plan = _get_org_plan(org_config)
    if org_plan not in ('free',):
        return
    limit = get_plan_limit(org_plan, 'admin_seats')
    if limit <= 0:
        return
    current = await _get_actual_admin_seat_count(org_id, db_session)
    if current + holders > limit:
        raise HTTPException(status_code=403, detail='Usage Limit has been reached for Admin_seats')

async def enforce_admin_seat_limit_for_role_change(org_id: int, user_id: int, new_role: Optional[Role], db_session: AsyncSession) -> None:
    """Enforce the admin-seat limit when ``user_id`` is being assigned
    ``new_role`` in ``org_id``.

    Only a NET-NEW seat is checked:
    - Granting a non-dashboard role (a demotion, or a normal-member role) never
      raises; you can always remove admins or add regular members.
    - Moving a user who already occupies an admin seat between two
      dashboard-access roles consumes no new seat, so it is not blocked.
    - Granting a dashboard-access role to a user who does not already hold one
      (a promotion, or a brand-new admin membership) is gated by
      ``check_admin_seat_limit`` and raises 403 for a free org at its limit.
    """
    if not _role_grants_dashboard_access(new_role):
        return
    if await _get_org_config(org_id, db_session) is None:
        return
    current = (await db_session.execute(select(UserOrganization).where(UserOrganization.org_id == org_id, UserOrganization.user_id == user_id))).scalars().first()
    if current is not None and current.role_id is not None:
        current_role = (await db_session.execute(select(Role).where(Role.id == current.role_id))).scalars().first()
        if _role_grants_dashboard_access(current_role):
            return
    await check_admin_seat_limit(org_id, db_session)

async def get_admin_seat_usage(org_id: int, db_session: AsyncSession) -> dict:
    """Get admin seat usage summary."""
    statement = select(OrganizationConfig).where(OrganizationConfig.org_id == org_id)
    org_config = (await db_session.execute(statement)).scalars().first()
    if org_config is None:
        return {'error': 'Organization has no config'}
    org_plan = _get_org_plan(org_config)
    current_usage = await _get_actual_admin_seat_count(org_id, db_session)
    limit = get_plan_limit(org_plan, 'admin_seats')
    return {'plan': org_plan, 'current_usage': current_usage, 'limit': limit if limit > 0 else 'unlimited', 'remaining': limit - current_usage if limit > 0 else 'unlimited'}

def is_role_dashboard_enabled(role: Role) -> bool:
    """Check if a role has dashboard access."""
    rights = role.rights
    if isinstance(rights, dict):
        dashboard = rights.get('dashboard', {})
        return dashboard.get('action_access', False)
    return False
