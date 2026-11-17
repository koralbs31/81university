from typing import Literal, List
from uuid import uuid4
from sqlmodel import select, text
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlalchemy.exc import IntegrityError
from src.security.rbac.rbac import authorization_verify_based_on_roles_and_authorship, authorization_verify_if_user_is_anon
from src.security.org_auth import require_org_role_permission, get_user_org_role
from src.security.superadmin import is_user_superadmin
from src.db.users import AnonymousUser, APITokenUser, PublicUser
from src.security.auth import resolve_acting_user_id
from src.db.roles import Rights, Role, RoleCreate, RoleRead, RoleUpdate, RoleTypeEnum
from src.db.organizations import Organization
from fastapi import HTTPException, Request
from datetime import datetime
from types import SimpleNamespace

async def create_role(request: Request, db_session: AsyncSession, role_object: RoleCreate, current_user: PublicUser | AnonymousUser | APITokenUser):
    role = Role.model_validate(role_object)
    await rbac_check(request, current_user, 'create', 'role_xxx', db_session)
    if not role.org_id:
        raise HTTPException(status_code=400, detail='Organization ID is required for role creation')
    role.role_type = RoleTypeEnum.TYPE_ORGANIZATION
    statement = select(Organization).where(Organization.id == role.org_id)
    organization = (await db_session.execute(statement)).scalars().first()
    if not organization:
        raise HTTPException(status_code=404, detail='Organization not found')
    await require_org_role_permission(resolve_acting_user_id(current_user), role.org_id, db_session, 'roles', 'action_create')
    statement = select(Role).where(Role.name == role.name, Role.org_id == role.org_id, Role.role_type == RoleTypeEnum.TYPE_ORGANIZATION)
    existing_role = (await db_session.execute(statement)).scalars().first()
    if existing_role:
        raise HTTPException(status_code=409, detail=f"A role with the name '{role.name}' already exists in this organization")
    if not role.name or role.name.strip() == '':
        raise HTTPException(status_code=400, detail='Role name is required and cannot be empty')
    if len(role.name.strip()) > 100:
        raise HTTPException(status_code=400, detail='Role name cannot exceed 100 characters')
    if role.rights:
        rights_dict = role.rights if isinstance(role.rights, dict) else role.rights.model_dump()
        _validate_rights_payload(rights_dict)
        role.rights = rights_dict
    create_role_user_id = resolve_acting_user_id(current_user)
    if not await is_user_superadmin(create_role_user_id, db_session):
        await _enforce_rights_subset(role.rights, create_role_user_id, role.org_id, db_session, "You cannot create a role with '{perm}' permission for '{bucket}' as you don't have this permission yourself")
    role.role_uuid = f'role_{uuid4()}'
    role.creation_date = str(datetime.now())
    role.update_date = str(datetime.now())
    try:
        db_session.add(role)
        await db_session.commit()
        await db_session.refresh(role)
    except IntegrityError as e:
        if 'duplicate key value violates unique constraint' in str(e) and 'role_pkey' in str(e):
            await db_session.rollback()
            result = await db_session.execute(text('SELECT COALESCE(MAX(id), 0) as max_id FROM role'))
            max_id_result = result.scalar()
            max_id = max_id_result if max_id_result is not None else 0
            role.id = max_id + 1
            db_session.add(role)
            await db_session.commit()
            await db_session.refresh(role)
            try:
                next_val = max_id + 1
                await db_session.execute(text("SELECT setval('role_id_seq', :next_val, true)"), {'next_val': next_val})
                await db_session.commit()
            except Exception:
                pass
        else:
            raise e
    role_data = role.model_dump()
    if role_data.get('org_id') is None:
        role_data['org_id'] = 0
    role = RoleRead(**role_data)
    return role

async def get_roles_by_organization(request: Request, db_session: AsyncSession, org_id: int, current_user: PublicUser | AnonymousUser | APITokenUser) -> List[RoleRead]:
    """
    Get all roles for a specific organization, including global roles.

    Args:
        request: FastAPI request object
        db_session: Database session
        org_id: Organization ID
        current_user: Current authenticated user

    Returns:
        List[RoleRead]: List of roles for the organization (including global roles)

    Raises:
        HTTPException: If organization not found or user lacks permissions
    """
    statement = select(Organization).where(Organization.id == org_id)
    organization = (await db_session.execute(statement)).scalars().first()
    if not organization:
        raise HTTPException(status_code=404, detail='Organization not found')
    await require_org_role_permission(resolve_acting_user_id(current_user), org_id, db_session, 'roles', 'action_read')
    global_roles_statement = select(Role).where(Role.role_type == RoleTypeEnum.TYPE_GLOBAL).order_by(Role.id)
    global_roles = list((await db_session.execute(global_roles_statement)).scalars().all())
    org_roles_statement = select(Role).where(Role.org_id == org_id, Role.role_type == RoleTypeEnum.TYPE_ORGANIZATION).order_by(Role.id)
    org_roles = list((await db_session.execute(org_roles_statement)).scalars().all())
    all_roles = global_roles + org_roles
    role_reads = []
    for role in all_roles:
        role_data = role.model_dump()
        if role_data.get('org_id') is None:
            role_data['org_id'] = 0
        role_reads.append(RoleRead(**role_data))
    return role_reads

async def read_role(request: Request, db_session: AsyncSession, role_id: int, current_user: PublicUser | AnonymousUser | APITokenUser):
    statement = select(Role).where(Role.id == role_id)
    result = await db_session.execute(statement)
    role = result.scalars().first()
    if not role:
        raise HTTPException(status_code=404, detail='Role not found')
    acting_user_id = resolve_acting_user_id(current_user)
    await authorization_verify_if_user_is_anon(acting_user_id)
    if role.org_id is not None:
        await require_org_role_permission(acting_user_id, role.org_id, db_session, 'roles', 'action_read')
    role = RoleRead(**role.model_dump())
    return role

async def update_role(request: Request, db_session: AsyncSession, role_object: RoleUpdate, current_user: PublicUser | AnonymousUser | APITokenUser):
    statement = select(Role).where(Role.id == role_object.role_id)
    result = await db_session.execute(statement)
    role = result.scalars().first()
    if not role:
        raise HTTPException(status_code=404, detail='Role not found')
    if role.role_type == RoleTypeEnum.TYPE_GLOBAL:
        raise HTTPException(status_code=403, detail='Global roles cannot be updated. These are system-defined roles that must remain unchanged.')
    await require_org_role_permission(resolve_acting_user_id(current_user), role.org_id, db_session, 'roles', 'action_update')
    from src.security.features_utils.usage import _role_grants_dashboard_access, enforce_admin_seat_limit_for_role_rights_change
    _seat_role_id = role.id
    _was_dashboard_role = _role_grants_dashboard_access(role)
    del role_object.role_id
    update_data = role_object.model_dump(exclude_unset=True)
    new_rights = update_data.get('rights')
    if new_rights:
        _validate_rights_payload(new_rights)
    update_role_user_id = resolve_acting_user_id(current_user)
    if 'rights' in update_data and (not await is_user_superadmin(update_role_user_id, db_session)):
        await _enforce_rights_subset(new_rights or {}, update_role_user_id, role.org_id, db_session, "You cannot grant a role the '{perm}' permission for '{bucket}' as you don't have this permission yourself")
    will_grant_dashboard = _role_grants_dashboard_access(SimpleNamespace(rights=new_rights)) if new_rights else _was_dashboard_role
    await enforce_admin_seat_limit_for_role_rights_change(role.org_id, _seat_role_id, will_grant_dashboard=will_grant_dashboard, currently_grants_dashboard=_was_dashboard_role, db_session=db_session)
    role.update_date = str(datetime.now())
    for key, value in update_data.items():
        if value is not None:
            setattr(role, key, value)
    if new_rights:
        role.rights = new_rights
    db_session.add(role)
    await db_session.commit()
    await db_session.refresh(role)
    from src.db.user_organizations import UserOrganization
    from src.routers.users import _invalidate_session_cache
    affected_users = (await db_session.execute(select(UserOrganization.user_id).where(UserOrganization.role_id == role.id))).scalars().all()
    for uid in affected_users:
        _invalidate_session_cache(uid)
    role = RoleRead(**role.model_dump())
    return role

async def delete_role(request: Request, db_session: AsyncSession, role_id: int, current_user: PublicUser | AnonymousUser | APITokenUser):
    statement = select(Role).where(Role.id == role_id)
    result = await db_session.execute(statement)
    role = result.scalars().first()
    if not role:
        raise HTTPException(status_code=404, detail='Role not found')
    if role.role_type == RoleTypeEnum.TYPE_GLOBAL:
        raise HTTPException(status_code=403, detail='Global roles cannot be deleted. These are system-defined roles that must remain unchanged.')
    await require_org_role_permission(resolve_acting_user_id(current_user), role.org_id, db_session, 'roles', 'action_delete')
    from src.db.user_organizations import UserOrganization
    from src.routers.users import _invalidate_session_cache
    affected_users = (await db_session.execute(select(UserOrganization.user_id).where(UserOrganization.role_id == role.id))).scalars().all()
    for uid in affected_users:
        _invalidate_session_cache(uid)
    await db_session.delete(role)
    await db_session.commit()
    return 'Role deleted'
REQUIRED_RIGHTS_BUCKETS = ('courses', 'users', 'usergroups', 'organizations', 'coursechapters', 'activities', 'roles', 'dashboard')

def _validate_rights_payload(rights_dict) -> None:
    """Reject a role rights payload that isn't exactly the Rights shape.

    Every bucket is checked, not just the required ones: an unvalidated bucket
    (or a truthy non-bool like ``1``) would slip past the escalation check
    below and still be honoured by the RBAC lookups.
    """
    if not isinstance(rights_dict, dict):
        raise HTTPException(status_code=400, detail='Rights must be a JSON object')
    for bucket in rights_dict:
        if bucket not in Rights.model_fields:
            raise HTTPException(status_code=400, detail=f'Unknown right: {bucket}')
    for required_right in REQUIRED_RIGHTS_BUCKETS:
        if required_right not in rights_dict:
            raise HTTPException(status_code=400, detail=f'Missing required right: {required_right}')
    for bucket, right_data in rights_dict.items():
        if not isinstance(right_data, dict):
            raise HTTPException(status_code=400, detail=f"Right '{bucket}' must be a JSON object")
        allowed_perms = Rights.model_fields[bucket].annotation.model_fields
        for perm in right_data:
            if perm not in allowed_perms:
                raise HTTPException(status_code=400, detail=f"Unknown permission '{perm}' for '{bucket}'")
        for perm in allowed_perms:
            if bucket == 'courses':
                missing = f'Missing required course permission: {perm}'
                not_bool = f"Course permission '{perm}' must be a boolean"
            elif bucket == 'dashboard':
                missing = f'Missing required dashboard permission: {perm}'
                not_bool = f"Dashboard permission '{perm}' must be a boolean"
            else:
                missing = f"Missing required permission '{perm}' for '{bucket}'"
                not_bool = f"Permission '{perm}' for '{bucket}' must be a boolean"
            if perm not in right_data:
                raise HTTPException(status_code=400, detail=missing)
            if not isinstance(right_data[perm], bool):
                raise HTTPException(status_code=400, detail=not_bool)

async def _enforce_rights_subset(role_rights, acting_user_id: int, org_id: int, db_session: AsyncSession, detail_template: str) -> None:
    """403 unless every permission ``role_rights`` grants is one the caller holds in the org."""
    if not role_rights or not isinstance(role_rights, dict):
        return
    user_role = await get_user_org_role(acting_user_id, org_id, db_session)
    held = user_role.rights if user_role and user_role.rights else {}
    if not isinstance(held, dict):
        held = held.model_dump()
    for bucket, perms in role_rights.items():
        if not isinstance(perms, dict):
            continue
        held_perms = held.get(bucket)
        for perm, value in perms.items():
            if value and (not (isinstance(held_perms, dict) and held_perms.get(perm))):
                raise HTTPException(status_code=403, detail=detail_template.format(perm=perm, bucket=bucket))

async def rbac_check(request: Request, current_user: PublicUser | AnonymousUser, action: Literal['create', 'read', 'update', 'delete'], role_uuid: str, db_session: AsyncSession):
    acting_user_id = resolve_acting_user_id(current_user)
    await authorization_verify_if_user_is_anon(acting_user_id)
    await authorization_verify_based_on_roles_and_authorship(request, acting_user_id, action, role_uuid, db_session)
