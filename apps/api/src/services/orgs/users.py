import csv
import io
import json
import logging
import re
from datetime import datetime, timedelta
from typing import Optional
import redis
from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import aliased
from sqlmodel import select, func
from sqlmodel.ext.asyncio.session import AsyncSession
from config.config import get_learnhouse_config
from src.db.organization_config import OrganizationConfig
from src.db.organizations import Organization, OrganizationRead, OrganizationUser
from src.db.roles import Role, RoleRead
from src.db.user_organizations import UserOrganization
from src.db.usergroup_user import UserGroupUser
from src.db.usergroups import UserGroup, UserGroupRead
from src.db.users import AnonymousUser, APITokenUser, InternalUser, OrgMemberUserRead, PublicUser, User, UserRead
from src.security.auth import resolve_acting_user_id
from src.security.features_utils.usage import check_members_limit_with_pending, decrease_feature_usage, enforce_admin_seat_limit_for_role_change
from src.services.security.account_age import enforce_free_tier_age_gate
from src.services.security.rate_limiting import INVITE_MAX_BATCH_SIZE, enforce_batch_size_limit, enforce_invite_rate_limit
from src.security.org_auth import enforce_org_mfa, get_user_org, is_org_member, require_org_destroy_right, require_org_role_permission
from src.security.rbac.constants import ADMIN_OR_MAINTAINER_ROLE_IDS, ADMIN_ROLE_ID
from src.services.orgs.invites import send_invite_email
from src.services.demo.guards import hide_other_visitors
from src.services.orgs.orgs import is_org_admin_role, rbac_check
from src.services.search.normalization import LIKE_ESCAPE_CHAR, build_like_pattern
from src.services.users.emails import send_role_changed_email
from src.services.webhooks.dispatch import dispatch_webhooks
from src.services.analytics import events as analytics_events
from src.services.analytics.analytics import track
logger = logging.getLogger(__name__)

def _csv_safe(value):
    """Neutralize CSV formula injection (S12).

    Spreadsheet apps interpret a cell whose first character is one of = + - @
    (or a leading tab / carriage return) as a formula. Prefix any such string
    with a single quote so it is rendered as literal text. Non-string values
    are returned unchanged.
    """
    if isinstance(value, str) and value and (value[0] in ('\t', '\r') or value.lstrip()[:1] in ('=', '+', '-', '@')):
        return "'" + value
    return value
_EMAIL_RE = re.compile('^[^@\\s:]{1,64}@[^@\\s:]{1,255}\\.[a-zA-Z]{2,}$')

def _hide_other_visitors(statement, org: Organization, acting_user_id: int):
    """Restrict a member listing to the seeded students plus the caller.

    The seeded students are the point of the members page, so they stay; other
    visitors do not. See ``hide_other_visitors`` for why.
    """
    if not org.is_demo:
        return statement
    return hide_other_visitors(statement, acting_user_id)

def _looks_like_email(value: str) -> bool:
    if not value or len(value) > 254:
        return False
    if any((ord(c) < 32 or ord(c) == 127 for c in value)):
        return False
    return bool(_EMAIL_RE.match(value))

def _rights_within(granted, held) -> bool:
    """Whether every permission ``granted`` turns on is also on in ``held``."""

    def as_dict(rights) -> dict:
        if rights is None:
            return {}
        return rights if isinstance(rights, dict) else rights.model_dump()
    held_rights = as_dict(held)
    for bucket, perms in as_dict(granted).items():
        if not isinstance(perms, dict):
            continue
        held_perms = held_rights.get(bucket)
        held_perms = held_perms if isinstance(held_perms, dict) else {}
        for perm, value in perms.items():
            if value is True and held_perms.get(perm) is not True:
                return False
    return True

async def _require_admin_to_touch_admins(request: Request, org: Organization, current_user: PublicUser | AnonymousUser | APITokenUser, db_session: AsyncSession) -> None:
    """Only Admins (or superadmins) may remove an Admin or Maintainer.

    rbac_check lets Maintainers remove ordinary members; removing staff also
    takes the users.delete right, which the seeded Maintainer role lacks.
    """
    if isinstance(current_user, (InternalUser, APITokenUser)):
        return
    if not await is_org_admin_role(current_user.id, org.id, db_session):
        raise HTTPException(status_code=403, detail='Only organization administrators can remove an administrator or maintainer')
    await require_org_role_permission(current_user.id, org.id, db_session, 'users', 'action_delete')

async def _require_rights_over_members(org: Organization, current_user: PublicUser | AnonymousUser | APITokenUser, user_ids: list[int], db_session: AsyncSession) -> None:
    """Below Admin, a caller may only act on members whose role grants nothing
    the caller's own role lacks.

    The seeded Admin/Maintainer ids are guarded separately; this covers custom
    roles, which can be more powerful than the Maintainer acting on them.
    """
    if isinstance(current_user, (InternalUser, APITokenUser)) or not user_ids:
        return
    from src.security.superadmin import is_user_superadmin
    acting_user_id = resolve_acting_user_id(current_user)
    if await is_user_superadmin(acting_user_id, db_session):
        return
    caller_membership = await get_user_org(acting_user_id, org.id, db_session)
    if caller_membership is not None and caller_membership.role_id == ADMIN_ROLE_ID:
        return
    caller_role = await db_session.get(Role, caller_membership.role_id) if caller_membership is not None else None
    target_rights = (await db_session.execute(select(Role.rights).join(UserOrganization, UserOrganization.role_id == Role.id).where(UserOrganization.org_id == org.id, UserOrganization.user_id.in_(user_ids)))).scalars().all()
    for rights in target_rights:
        if caller_role is None or not _rights_within(rights, caller_role.rights):
            raise HTTPException(status_code=403, detail="You cannot manage a member whose role has permissions you don't have")

async def _revoke_org_grants(db_session: AsyncSession, org: Organization, user_ids: list[int]) -> None:
    """Drop the per-resource grants departing members hold in ``org``.

    Deleting the UserOrganization row alone leaves authorship, usergroup and
    board membership plus their API tokens and connected AI apps in place,
    each of which keeps granting access. Staged on the session; the caller commits.
    """
    if not user_ids:
        return
    from sqlalchemy import delete, update, union_all
    from src.db.api_tokens import APIToken
    from src.db.courses.courses import Course
    from src.db.resource_authors import ResourceAuthor, ResourceAuthorshipStatusEnum
    now = str(datetime.now())
    org_resource_uuids = union_all(select(Course.course_uuid).where(Course.org_id == org.id))
    await db_session.execute(update(ResourceAuthor).where(ResourceAuthor.user_id.in_(user_ids), ResourceAuthor.resource_uuid.in_(org_resource_uuids)).values(authorship_status=ResourceAuthorshipStatusEnum.INACTIVE, update_date=now))
    await db_session.execute(delete(UserGroupUser).where(UserGroupUser.user_id.in_(user_ids), UserGroupUser.usergroup_id.in_(select(UserGroup.id).where(UserGroup.org_id == org.id))))
    await db_session.execute(update(APIToken).where(APIToken.created_by_user_id.in_(user_ids), APIToken.org_id == org.id, APIToken.is_active == True).values(is_active=False, update_date=now))

async def _drop_accepted_invites(db_session: AsyncSession, org: Organization, user_ids: list[int]) -> None:
    """Remove the invite records that would let a removed member rejoin an
    invite-only org (the OAuth signup gate accepts any existing key)."""
    if not user_ids:
        return
    from src.core.redis import get_redis_client
    r = get_redis_client()
    if r is None:
        return
    emails = (await db_session.execute(select(User.email).where(User.id.in_(user_ids)))).scalars().all()
    try:
        keys = [f'invited_user:{email.strip().lower()}:org:{org.org_uuid}' for email in emails if email]
        if keys:
            r.delete(*keys)
    except Exception:
        logger.warning('Failed to drop invites for org %s', org.id, exc_info=True)

async def get_organization_users(request: Request, org_id: int, db_session: AsyncSession, current_user: PublicUser | AnonymousUser | APITokenUser, page: int=1, limit: int=20, search: str='', usergroup_id: int | None=None, usergroup_filter: str | None=None, sort_order: str='desc', role_id: int | None=None, status: str | None=None):
    """
    Get paginated list of users in an organization.

    SECURITY:
    - Requires authentication (enforced at router level)
    - User must be a member of the organization to view member list
    - Maximum limit enforced to prevent data dumping
    """
    limit = min(limit, 100)
    page = max(page, 1)
    statement = select(Organization).where(Organization.id == org_id)
    org = (await db_session.execute(statement)).scalars().first()
    if not org:
        raise HTTPException(status_code=404, detail='Organization not found')
    if isinstance(current_user, AnonymousUser):
        raise HTTPException(status_code=401, detail='Authentication required')
    acting_user_id = resolve_acting_user_id(current_user)
    if not await is_org_member(acting_user_id, org.id, db_session):
        raise HTTPException(status_code=403, detail='You must be a member of this organization to view its members')
    await enforce_org_mfa(acting_user_id, org.id, db_session)
    from src.security.superadmin import is_user_superadmin
    if not await is_user_superadmin(acting_user_id, db_session):
        from src.security.org_auth import is_org_admin
        if not await is_org_admin(acting_user_id, org.id, db_session):
            raise HTTPException(status_code=403, detail='Only administrators and maintainers can view organization members')
        await enforce_org_mfa(acting_user_id, org.id, db_session)
    base_statement = select(User).join(UserOrganization).join(Organization).where(Organization.id == org_id)
    base_statement = _hide_other_visitors(base_statement, org, acting_user_id)
    if search:
        search_pattern = build_like_pattern(search)
        base_statement = base_statement.where(User.first_name.ilike(search_pattern, escape=LIKE_ESCAPE_CHAR) | User.last_name.ilike(search_pattern, escape=LIKE_ESCAPE_CHAR) | User.username.ilike(search_pattern, escape=LIKE_ESCAPE_CHAR) | User.email.ilike(search_pattern, escape=LIKE_ESCAPE_CHAR))
    if role_id is not None:
        base_statement = base_statement.where(UserOrganization.role_id == role_id)
    if status == 'verified':
        base_statement = base_statement.where(User.email_verified == True)
    elif status == 'unverified':
        base_statement = base_statement.where(User.email_verified == False)
    in_group_total = None
    all_total = None
    if usergroup_id is not None:
        all_count_stmt = select(func.count()).select_from(base_statement.subquery())
        all_total = (await db_session.execute(all_count_stmt)).scalar_one()
        in_group_count_stmt = select(func.count(User.id)).join(UserOrganization).join(Organization).where(Organization.id == org_id).join(UserGroupUser, (UserGroupUser.user_id == User.id) & (UserGroupUser.usergroup_id == usergroup_id))
        in_group_count_stmt = _hide_other_visitors(in_group_count_stmt, org, acting_user_id)
        if search:
            search_pattern = build_like_pattern(search)
            in_group_count_stmt = in_group_count_stmt.where(User.first_name.ilike(search_pattern, escape=LIKE_ESCAPE_CHAR) | User.last_name.ilike(search_pattern, escape=LIKE_ESCAPE_CHAR) | User.username.ilike(search_pattern, escape=LIKE_ESCAPE_CHAR) | User.email.ilike(search_pattern, escape=LIKE_ESCAPE_CHAR))
        in_group_total = (await db_session.execute(in_group_count_stmt)).scalar_one()
    if usergroup_id is not None and usergroup_filter:
        if usergroup_filter == 'in_group':
            base_statement = base_statement.join(UserGroupUser, (UserGroupUser.user_id == User.id) & (UserGroupUser.usergroup_id == usergroup_id))
        elif usergroup_filter == 'not_in_group':
            ugu_alias = aliased(UserGroupUser)
            base_statement = base_statement.outerjoin(ugu_alias, (ugu_alias.user_id == User.id) & (ugu_alias.usergroup_id == usergroup_id)).where(ugu_alias.id.is_(None))
    total = (await db_session.execute(select(func.count()).select_from(base_statement.subquery()))).scalar_one()
    if sort_order == 'asc':
        base_statement = base_statement.order_by(UserOrganization.id.asc())
    else:
        base_statement = base_statement.order_by(UserOrganization.id.desc())
    offset = (page - 1) * limit
    paginated_statement = base_statement.offset(offset).limit(limit)
    users = (await db_session.execute(paginated_statement)).scalars().all()
    org_users_list = []
    user_ids = [user.id for user in users]
    user_orgs_statement = select(UserOrganization).where(UserOrganization.user_id.in_(user_ids), UserOrganization.org_id == org_id)
    user_orgs = (await db_session.execute(user_orgs_statement)).scalars().all()
    user_org_map = {uo.user_id: uo for uo in user_orgs}
    role_ids = list({uo.role_id for uo in user_orgs if uo.role_id is not None})
    if role_ids:
        roles_statement = select(Role).where(Role.id.in_(role_ids))
        roles = (await db_session.execute(roles_statement)).scalars().all()
        role_map = {role.id: role for role in roles}
    else:
        role_map = {}
    usergroups_statement = select(UserGroupUser, UserGroup).join(UserGroup, UserGroupUser.usergroup_id == UserGroup.id).where(UserGroupUser.user_id.in_(user_ids), UserGroupUser.org_id == org_id)
    usergroup_results = (await db_session.execute(usergroups_statement)).all()
    user_usergroups_map: dict[int, list[UserGroupRead]] = {}
    for ugu, ug in usergroup_results:
        user_usergroups_map.setdefault(ugu.user_id, []).append(UserGroupRead.model_validate(ug))
    from src.security.features_utils.active_users import get_visit_days_for_users, current_utc_year_month, ACTIVE_DAYS_THRESHOLD
    visit_days_map: dict[int, int] = {}
    try:
        _year, _month = current_utc_year_month()
        visit_days_map = await get_visit_days_for_users(org_id, [uid for uid in user_ids if uid is not None], _year, _month, db_session)
    except Exception:
        logging.debug('visit_days enrichment unavailable', exc_info=True)
    from src.services.orgs.signup_fields import get_org_signup_fields
    member_meta_keys: set[str] | None = None
    for user in users:
        user_org = user_org_map.get(user.id)
        if not user_org:
            logging.error(f'User {user.id} not found')
            continue
        role = role_map.get(user_org.role_id)
        if not role:
            logging.error(f'Role {user_org.role_id} not found')
            continue
        if member_meta_keys is None:
            member_meta_keys = {f.key for f in await get_org_signup_fields(org_id, db_session)}
        user_read = OrgMemberUserRead.for_org(user, member_meta_keys)
        role_read = RoleRead.model_validate(role)
        usergroups = user_usergroups_map.get(user.id, [])
        _days = visit_days_map.get(user.id, 0)
        org_user = OrganizationUser(user=user_read, role=role_read, usergroups=usergroups, joined_at=user_org.creation_date, visit_days=_days, is_active=_days >= ACTIVE_DAYS_THRESHOLD)
        org_users_list.append(org_user)
    result = {'items': org_users_list, 'total': total, 'page': page, 'limit': limit}
    if in_group_total is not None:
        result['in_group_total'] = in_group_total
    if all_total is not None:
        result['all_total'] = all_total
    return result

async def export_organization_users_csv(request: Request, org_id: int, db_session: AsyncSession, current_user: PublicUser | AnonymousUser, search: str='', usergroup_id: int | None=None, usergroup_filter: str | None=None, sort_order: str='desc', role_id: int | None=None, status: str | None=None):
    """
    Export all organization users as CSV.
    Reuses the same auth/filtering logic as get_organization_users but without pagination.
    """
    if isinstance(current_user, AnonymousUser):
        raise HTTPException(status_code=401, detail='Authentication required')
    statement = select(Organization).where(Organization.id == org_id)
    org = (await db_session.execute(statement)).scalars().first()
    if not org:
        raise HTTPException(status_code=404, detail='Organization not found')
    export_acting_user_id = resolve_acting_user_id(current_user)
    if not await is_org_member(export_acting_user_id, org.id, db_session):
        raise HTTPException(status_code=403, detail='You must be a member of this organization')
    await enforce_org_mfa(export_acting_user_id, org.id, db_session)
    from src.security.superadmin import is_user_superadmin
    if not await is_user_superadmin(export_acting_user_id, db_session):
        from src.security.org_auth import is_org_admin
        if not await is_org_admin(export_acting_user_id, org.id, db_session):
            raise HTTPException(status_code=403, detail='Only administrators and maintainers can export organization members')
        await enforce_org_mfa(export_acting_user_id, org.id, db_session)
    base_statement = select(User).join(UserOrganization).join(Organization).where(Organization.id == org_id)
    base_statement = _hide_other_visitors(base_statement, org, export_acting_user_id)
    if search:
        search_pattern = build_like_pattern(search)
        base_statement = base_statement.where(User.first_name.ilike(search_pattern, escape=LIKE_ESCAPE_CHAR) | User.last_name.ilike(search_pattern, escape=LIKE_ESCAPE_CHAR) | User.username.ilike(search_pattern, escape=LIKE_ESCAPE_CHAR) | User.email.ilike(search_pattern, escape=LIKE_ESCAPE_CHAR))
    if role_id is not None:
        base_statement = base_statement.where(UserOrganization.role_id == role_id)
    if status == 'verified':
        base_statement = base_statement.where(User.email_verified == True)
    elif status == 'unverified':
        base_statement = base_statement.where(User.email_verified == False)
    if usergroup_id is not None and usergroup_filter:
        if usergroup_filter == 'in_group':
            base_statement = base_statement.join(UserGroupUser, (UserGroupUser.user_id == User.id) & (UserGroupUser.usergroup_id == usergroup_id))
        elif usergroup_filter == 'not_in_group':
            ugu_alias = aliased(UserGroupUser)
            base_statement = base_statement.outerjoin(ugu_alias, (ugu_alias.user_id == User.id) & (ugu_alias.usergroup_id == usergroup_id)).where(ugu_alias.id.is_(None))
    if sort_order == 'asc':
        base_statement = base_statement.order_by(UserOrganization.id.asc())
    else:
        base_statement = base_statement.order_by(UserOrganization.id.desc())
    users = (await db_session.execute(base_statement)).scalars().all()
    from src.services.orgs.signup_fields import get_signup_fields
    org_config_row = (await db_session.execute(select(OrganizationConfig).where(OrganizationConfig.org_id == org_id))).scalars().first()
    custom_fields = get_signup_fields(org_config_row.config if org_config_row else None)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Name', 'Username', 'Email', 'Groups', 'Role', 'Joined', 'Email Verified', 'Signup Method', 'Last Login'] + [_csv_safe(f.label or f.key) for f in custom_fields])
    if users:
        user_ids = [user.id for user in users]
        user_orgs = (await db_session.execute(select(UserOrganization).where(UserOrganization.user_id.in_(user_ids), UserOrganization.org_id == org_id))).scalars().all()
        user_org_map = {uo.user_id: uo for uo in user_orgs}
        role_ids = list({uo.role_id for uo in user_orgs if uo.role_id is not None})
        role_map = {}
        if role_ids:
            roles = (await db_session.execute(select(Role).where(Role.id.in_(role_ids)))).scalars().all()
            role_map = {role.id: role for role in roles}
        usergroup_results = (await db_session.execute(select(UserGroupUser, UserGroup).join(UserGroup, UserGroupUser.usergroup_id == UserGroup.id).where(UserGroupUser.user_id.in_(user_ids), UserGroupUser.org_id == org_id))).all()
        user_usergroups_map: dict[int, list[str]] = {}
        for ugu, ug in usergroup_results:
            user_usergroups_map.setdefault(ugu.user_id, []).append(ug.name)

        def fmt_date(d):
            if not d:
                return ''
            try:
                dt = datetime.fromisoformat(str(d)) if not isinstance(d, datetime) else d
                return dt.strftime('%b %d, %Y')
            except Exception:
                return str(d)
        for user in users:
            user_org = user_org_map.get(user.id)
            if not user_org:
                continue
            role = role_map.get(user_org.role_id)
            groups = '; '.join(user_usergroups_map.get(user.id, []))
            metadata = user.extra_metadata or {}
            custom_values = []
            for field in custom_fields:
                value = metadata.get(field.key)
                if value is None:
                    custom_values.append('')
                elif isinstance(value, bool):
                    custom_values.append('Yes' if value else 'No')
                else:
                    custom_values.append(_csv_safe(str(value)))
            writer.writerow([_csv_safe(f"{user.first_name or ''} {user.last_name or ''}".strip()), _csv_safe(user.username or ''), _csv_safe(user.email or ''), _csv_safe(groups), _csv_safe(role.name if role else ''), fmt_date(user_org.creation_date), 'Yes' if user.email_verified else 'No', _csv_safe(user.signup_method or ''), fmt_date(user.last_login_at) if hasattr(user, 'last_login_at') else ''] + custom_values)
    output.seek(0)
    return StreamingResponse(output, media_type='text/csv', headers={'Content-Disposition': 'attachment; filename=users-export.csv'})


async def remove_user_from_org(request: Request, org_id: int, user_id: int, db_session: AsyncSession, current_user: PublicUser | AnonymousUser):
    statement = select(Organization).where(Organization.id == org_id)
    org = (await db_session.execute(statement)).scalars().first()
    if not org:
        raise HTTPException(status_code=404, detail='Organization not found')
    await rbac_check(request, org.org_uuid, current_user, 'delete', db_session)
    statement = select(UserOrganization).where(UserOrganization.user_id == user_id, UserOrganization.org_id == org.id)
    user_org = (await db_session.execute(statement)).scalars().first()
    if not user_org:
        raise HTTPException(status_code=404, detail='User not found')
    if user_org.role_id in ADMIN_OR_MAINTAINER_ROLE_IDS:
        await _require_admin_to_touch_admins(request, org, current_user, db_session)
    await _require_rights_over_members(org, current_user, [user_id], db_session)
    statement = select(UserOrganization).where(UserOrganization.org_id == org.id, UserOrganization.role_id == ADMIN_ROLE_ID)
    admins = (await db_session.execute(statement)).scalars().all()
    if len(admins) == 1 and admins[0].user_id == user_id:
        raise HTTPException(status_code=400, detail="You can't remove the last admin of the organization")
    await db_session.delete(user_org)
    await _revoke_org_grants(db_session, org, [user_id])
    await db_session.commit()
    await _drop_accepted_invites(db_session, org, [user_id])
    from src.routers.users import _invalidate_session_cache
    _invalidate_session_cache(user_id)
    await decrease_feature_usage('members', org_id, db_session)
    await track(event_name=analytics_events.USER_REMOVED_FROM_ORG, org_id=org_id, user_id=user_id)
    await dispatch_webhooks(event_name='user_removed_from_org', org_id=org_id, data={'user_id': user_id, 'org_id': org_id})
    return {'detail': 'User removed from org'}

async def leave_org(request: Request, org_id: int, db_session: AsyncSession, current_user: PublicUser):
    """Let the CURRENT user leave an org they belong to (self-service, no admin
    rights needed). They can only remove their OWN membership; the acting user
    id comes from the authenticated session, never a request field."""
    user_id = current_user.id
    org = (await db_session.execute(select(Organization).where(Organization.id == org_id))).scalars().first()
    if not org:
        raise HTTPException(status_code=404, detail='Organization not found')
    user_org = (await db_session.execute(select(UserOrganization).where(UserOrganization.user_id == user_id, UserOrganization.org_id == org.id))).scalars().first()
    if not user_org:
        raise HTTPException(status_code=404, detail='You are not a member of this organization')
    admins = (await db_session.execute(select(UserOrganization).where(UserOrganization.org_id == org.id, UserOrganization.role_id == ADMIN_ROLE_ID))).scalars().all()
    if len(admins) == 1 and admins[0].user_id == user_id:
        raise HTTPException(status_code=400, detail='You are the last admin of this organization. Transfer ownership or delete the organization instead.')
    await db_session.delete(user_org)
    await _revoke_org_grants(db_session, org, [user_id])
    await db_session.commit()
    await _drop_accepted_invites(db_session, org, [user_id])
    from src.routers.users import _invalidate_session_cache
    _invalidate_session_cache(user_id)
    await decrease_feature_usage('members', org_id, db_session)
    await track(event_name=analytics_events.USER_REMOVED_FROM_ORG, org_id=org_id, user_id=user_id)
    await dispatch_webhooks(event_name='user_removed_from_org', org_id=org_id, data={'user_id': user_id, 'org_id': org_id, 'left': True})
    return {'detail': 'Left organization', 'org_id': org_id}

async def remove_batch_users_from_org(request: Request, org_id: int, user_ids: list[int], db_session: AsyncSession, current_user: PublicUser | AnonymousUser):
    statement = select(Organization).where(Organization.id == org_id)
    org = (await db_session.execute(statement)).scalars().first()
    if not org:
        raise HTTPException(status_code=404, detail='Organization not found')
    await rbac_check(request, org.org_uuid, current_user, 'delete', db_session)
    enforce_batch_size_limit(len(user_ids), 'users')
    admin_statement = select(UserOrganization).where(UserOrganization.org_id == org.id, UserOrganization.role_id == ADMIN_ROLE_ID)
    admins = (await db_session.execute(admin_statement)).scalars().all()
    admin_ids = {a.user_id for a in admins}
    staff_ids = set((await db_session.execute(select(UserOrganization.user_id).where(UserOrganization.org_id == org.id, UserOrganization.role_id.in_(ADMIN_OR_MAINTAINER_ROLE_IDS)))).scalars().all())
    if staff_ids & set(user_ids):
        await _require_admin_to_touch_admins(request, org, current_user, db_session)
    await _require_rights_over_members(org, current_user, user_ids, db_session)
    remaining_admins = admin_ids - set(user_ids)
    if len(admin_ids) > 0 and len(remaining_admins) == 0:
        raise HTTPException(status_code=400, detail='Cannot remove all admins from the organization')
    if user_ids:
        remove_stmt = select(UserOrganization).where(UserOrganization.user_id.in_(user_ids), UserOrganization.org_id == org.id)
        user_orgs_to_remove = (await db_session.execute(remove_stmt)).scalars().all()
    else:
        user_orgs_to_remove = []
    removed_count = len(user_orgs_to_remove)
    removed_user_ids = [uo.user_id for uo in user_orgs_to_remove]
    for user_org in user_orgs_to_remove:
        await db_session.delete(user_org)
    await _revoke_org_grants(db_session, org, removed_user_ids)
    await db_session.commit()
    await _drop_accepted_invites(db_session, org, removed_user_ids)
    from src.routers.users import _invalidate_session_cache
    for uid in user_ids:
        _invalidate_session_cache(uid)
    for _ in range(removed_count):
        await decrease_feature_usage('members', org_id, db_session)
    for uid in removed_user_ids:
        await track(event_name=analytics_events.USER_REMOVED_FROM_ORG, org_id=org_id, user_id=uid)
    return {'detail': f'{removed_count} user(s) removed from org'}

async def remove_all_users_from_org(request: Request, org_id: int, db_session: AsyncSession, current_user: PublicUser | AnonymousUser):
    """
    Remove every member from the organization except the caller.

    The organization and all of its content are kept intact. The acting admin
    is preserved so the org is never left without an administrator. Use the
    delete-organization endpoint to remove the org entirely.
    """
    statement = select(Organization).where(Organization.id == org_id)
    org = (await db_session.execute(statement)).scalars().first()
    if not org:
        raise HTTPException(status_code=404, detail='Organization not found')
    await rbac_check(request, org.org_uuid, current_user, 'delete', db_session)
    await require_org_destroy_right(resolve_acting_user_id(current_user), org.id, db_session)
    keep_user_id = resolve_acting_user_id(current_user)
    remove_stmt = select(UserOrganization).where(UserOrganization.org_id == org.id, UserOrganization.user_id != keep_user_id)
    user_orgs_to_remove = (await db_session.execute(remove_stmt)).scalars().all()
    removed_user_ids = [uo.user_id for uo in user_orgs_to_remove]
    for user_org in user_orgs_to_remove:
        await db_session.delete(user_org)
    await _revoke_org_grants(db_session, org, removed_user_ids)
    await db_session.commit()
    await _drop_accepted_invites(db_session, org, removed_user_ids)
    from src.routers.users import _invalidate_session_cache
    for uid in removed_user_ids:
        _invalidate_session_cache(uid)
    for _ in removed_user_ids:
        await decrease_feature_usage('members', org_id, db_session)
    return {'detail': f'{len(removed_user_ids)} user(s) removed from org'}

async def update_user_role(request: Request, org_id: int, user_id: int, role_uuid: str, db_session: AsyncSession, current_user: PublicUser | AnonymousUser):
    statement = select(Role).where(Role.role_uuid == role_uuid)
    role = (await db_session.execute(statement)).scalars().first()
    if not role:
        raise HTTPException(status_code=404, detail='Role not found')
    role_id = role.id
    statement = select(Organization).where(Organization.id == org_id)
    org = (await db_session.execute(statement)).scalars().first()
    if not org:
        raise HTTPException(status_code=404, detail='Organization not found')
    if role.org_id is not None and role.org_id != org.id:
        raise HTTPException(status_code=404, detail='Role not found')
    await rbac_check(request, org.org_uuid, current_user, 'update', db_session)
    from src.security.superadmin import is_user_superadmin
    if not isinstance(current_user, (InternalUser, APITokenUser)) and current_user.id == user_id and (not await is_user_superadmin(current_user.id, db_session)):
        raise HTTPException(status_code=403, detail='You cannot change your own role')
    acting_user_id = resolve_acting_user_id(current_user)
    if not isinstance(current_user, InternalUser) and (not await is_user_superadmin(acting_user_id, db_session)):
        caller_membership = await get_user_org(acting_user_id, org.id, db_session)
        if caller_membership is None or caller_membership.role_id != ADMIN_ROLE_ID:
            if role_id in ADMIN_OR_MAINTAINER_ROLE_IDS:
                raise HTTPException(status_code=403, detail='Only organization administrators can assign the Admin or Maintainer role')
            target_membership = await get_user_org(user_id, org.id, db_session)
            if target_membership is not None and target_membership.role_id in ADMIN_OR_MAINTAINER_ROLE_IDS:
                raise HTTPException(status_code=403, detail="Only organization administrators can change an Admin's or Maintainer's role")
            await _require_rights_over_members(org, current_user, [user_id], db_session)
            caller_role = await db_session.get(Role, caller_membership.role_id) if caller_membership is not None else None
            if caller_role is None or not _rights_within(role.rights, caller_role.rights):
                raise HTTPException(status_code=403, detail="You cannot assign a role with permissions you don't have")
    statement = select(UserOrganization).where(UserOrganization.org_id == org.id, UserOrganization.role_id == ADMIN_ROLE_ID)
    admins = (await db_session.execute(statement)).scalars().all()
    if not admins:
        raise HTTPException(status_code=400, detail='There is no admin in the organization')
    if len(admins) == 1 and admins[0].user_id == user_id and (str(role_uuid) != 'role_global_admin'):
        raise HTTPException(status_code=400, detail='Organization must have at least one admin')
    statement = select(UserOrganization).where(UserOrganization.user_id == user_id, UserOrganization.org_id == org.id)
    user_org = (await db_session.execute(statement)).scalars().first()
    if not user_org:
        raise HTTPException(status_code=404, detail='User not found')
    await enforce_admin_seat_limit_for_role_change(org.id, user_id, role, db_session)
    if role_id is not None:
        user_org.role_id = role_id
    db_session.add(user_org)
    await db_session.commit()
    await db_session.refresh(user_org)
    from src.routers.users import _invalidate_session_cache
    _invalidate_session_cache(user_org.user_id)
    await dispatch_webhooks(event_name='user_role_changed', org_id=org_id, data={'user_id': user_id, 'org_id': org_id, 'new_role_uuid': role_uuid})
    role_change_user = None
    try:
        user_stmt = select(User).where(User.id == user_id)
        role_change_user = (await db_session.execute(user_stmt)).scalars().first()
        if role_change_user and role_change_user.email and (not org.is_demo):
            org_config_stmt = select(OrganizationConfig).where(OrganizationConfig.org_id == org.id)
            org_config = (await db_session.execute(org_config_stmt)).scalars().first()
            from src.services.email.utils import get_org_signup_base_url
            org_base_url = await get_org_signup_base_url(org.slug, request, db_session=db_session, org_id=org.id)
            from src.services.email.branding import resolve_org_email_branding
            send_role_changed_email(email=role_change_user.email, username=role_change_user.username, org_name=org.name, new_role_name=role.name, cta_url=org_base_url.rstrip('/') or '/', **resolve_org_email_branding(org, org_config, request).as_kwargs())
    except Exception:
        logger.warning('Failed to send role change email to user %s', user_id)
    if role_id == ADMIN_ROLE_ID and (not org.is_demo):
        try:
            from src.services.marketing.loops import record_org_admin_in_loops
            if role_change_user and role_change_user.email:
                record_org_admin_in_loops(email=role_change_user.email, org_slug=org.slug, first_name=getattr(role_change_user, 'first_name', None), last_name=getattr(role_change_user, 'last_name', None))
        except Exception:
            logger.warning('record_org_admin_in_loops failed for user %s', user_id)
    return {'detail': 'User role updated'}

async def invite_batch_users(request: Request, org_id: int, emails: str, invite_code_uuid: Optional[str], db_session: AsyncSession, current_user: PublicUser | AnonymousUser):
    LH_CONFIG = get_learnhouse_config()
    redis_conn_string = LH_CONFIG.redis_config.redis_connection_string
    if not redis_conn_string:
        raise HTTPException(status_code=500, detail='Redis connection string not found')
    statement = select(Organization).where(Organization.id == org_id)
    org = (await db_session.execute(statement)).scalars().first()
    if not org:
        raise HTTPException(status_code=404, detail='Organization not found')
    await rbac_check(request, org.org_uuid, current_user, 'create', db_session)
    from src.services.demo.guards import require_not_demo_org
    await require_not_demo_org(org_id, db_session)
    acting_user_id = resolve_acting_user_id(current_user)
    user = await db_session.get(User, acting_user_id) if acting_user_id else None
    if user is None:
        raise HTTPException(status_code=403, detail='Invitations must come from a member')
    await enforce_free_tier_age_gate(org.id, acting_user_id, db_session, action='invite members')
    r = redis.Redis.from_url(redis_conn_string)
    if not r:
        raise HTTPException(status_code=500, detail='Could not connect to Redis')
    seen_emails = set()
    invite_list = []
    invalid_results = []
    for raw_email in emails.split(','):
        candidate = raw_email.strip()
        if not candidate or candidate.lower() in seen_emails:
            continue
        seen_emails.add(candidate.lower())
        if not _looks_like_email(candidate):
            invalid_results.append({'email': candidate, 'status': 'invalid_email'})
            continue
        invite_list.append(candidate.lower())
    if len(invite_list) > INVITE_MAX_BATCH_SIZE:
        raise HTTPException(status_code=400, detail={'code': 'INVITE_BATCH_TOO_LARGE', 'message': f'You can invite at most {INVITE_MAX_BATCH_SIZE} people at a time.', 'max_batch_size': INVITE_MAX_BATCH_SIZE})
    new_emails = [e for e in invite_list if not r.get(f'invited_user:{e}:org:{org.org_uuid}')]
    existing_pending = 0
    for key in r.scan_iter(match=f'invited_user:*:org:{org.org_uuid}', count=1000):
        try:
            record = json.loads(r.get(key))
            if record.get('pending', True):
                existing_pending += 1
        except Exception:
            existing_pending += 1
    await check_members_limit_with_pending(org.id, existing_pending + len(new_emails), db_session)
    if new_emails:
        enforce_invite_rate_limit(org.id, acting_user_id, len(new_emails))
    ttl = int(timedelta(days=60).total_seconds())
    results = []
    for email in invite_list:
        invited_user = r.get(f'invited_user:{email}:org:{org.org_uuid}')
        if invited_user:
            results.append({'email': email, 'status': 'already_invited'})
            continue
        org = OrganizationRead.model_validate(org)
        user = UserRead.model_validate(user)
        isEmailSent = await send_invite_email(org, invite_code_uuid, user, email, request, db_session=db_session)
        invited_user_object = {'email': email, 'org_id': org.id, 'invite_code_uuid': invite_code_uuid, 'pending': True, 'email_sent': isEmailSent, 'expires': ttl, 'created_at': datetime.now().isoformat(), 'created_by': user.user_uuid}
        r.set(f'invited_user:{email}:org:{org.org_uuid}', json.dumps(invited_user_object), ex=ttl)
        results.append({'email': email, 'status': 'sent' if isEmailSent else 'email_failed'})
    results.extend(invalid_results)
    sent = sum((1 for r in results if r['status'] == 'sent'))
    failed = sum((1 for r in results if r['status'] == 'email_failed'))
    skipped = sum((1 for r in results if r['status'] == 'already_invited'))
    invalid = sum((1 for r in results if r['status'] == 'invalid_email'))
    if sent + failed:
        await track(event_name=analytics_events.USER_INVITED, org_id=org_id, user_id=acting_user_id, properties={'invite_method': 'email', 'count': sent + failed})
    await dispatch_webhooks(event_name='user_invited_to_org', org_id=org_id, data={'org_id': org_id, 'emails': invite_list, 'invite_code_uuid': invite_code_uuid, 'invited_by': user.user_uuid})
    return {'detail': 'Users invited', 'results': results, 'summary': {'total': len(results), 'sent': sent, 'failed': failed, 'already_invited': skipped, 'invalid_email': invalid}}

async def get_list_of_invited_users(request: Request, org_id: int, db_session: AsyncSession, current_user: PublicUser | AnonymousUser):
    LH_CONFIG = get_learnhouse_config()
    redis_conn_string = LH_CONFIG.redis_config.redis_connection_string
    if not redis_conn_string:
        raise HTTPException(status_code=500, detail='Redis connection string not found')
    statement = select(Organization).where(Organization.id == org_id)
    org = (await db_session.execute(statement)).scalars().first()
    if not org:
        raise HTTPException(status_code=404, detail='Organization not found')
    if isinstance(current_user, AnonymousUser):
        raise HTTPException(status_code=401, detail='Authentication required')
    acting_user_id = resolve_acting_user_id(current_user)
    if not await is_org_member(acting_user_id, org.id, db_session):
        raise HTTPException(status_code=403, detail='You must be a member of this organization to view its invites')
    await enforce_org_mfa(acting_user_id, org.id, db_session)
    from src.security.superadmin import is_user_superadmin
    if not await is_user_superadmin(acting_user_id, db_session):
        from src.security.org_auth import is_org_admin
        if not await is_org_admin(acting_user_id, org.id, db_session):
            raise HTTPException(status_code=403, detail='Only administrators and maintainers can view organization invites')
    r = redis.Redis.from_url(redis_conn_string)
    if not r:
        raise HTTPException(status_code=500, detail='Could not connect to Redis')
    invited_users = list(r.scan_iter(match=f'invited_user:*:org:{org.org_uuid}', count=100))
    invited_users_list = []
    for user in invited_users:
        invited_user = r.get(user)
        if invited_user:
            invited_user = json.loads(invited_user.decode('utf-8'))
            invited_users_list.append(invited_user)
    return invited_users_list

async def remove_invited_user(request: Request, org_id: int, email: str, db_session: AsyncSession, current_user: PublicUser | AnonymousUser):
    LH_CONFIG = get_learnhouse_config()
    redis_conn_string = LH_CONFIG.redis_config.redis_connection_string
    if not redis_conn_string:
        raise HTTPException(status_code=500, detail='Redis connection string not found')
    statement = select(Organization).where(Organization.id == org_id)
    org = (await db_session.execute(statement)).scalars().first()
    if not org:
        raise HTTPException(status_code=404, detail='Organization not found')
    await rbac_check(request, org.org_uuid, current_user, 'delete', db_session)
    r = redis.Redis.from_url(redis_conn_string)
    if not r:
        raise HTTPException(status_code=500, detail='Could not connect to Redis')
    email = email.strip().lower()
    invited_user = r.get(f'invited_user:{email}:org:{org.org_uuid}')
    if not invited_user:
        raise HTTPException(status_code=404, detail='User not found')
    r.delete(f'invited_user:{email}:org:{org.org_uuid}')
    return {'detail': 'User removed'}
