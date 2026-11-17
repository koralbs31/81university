import secrets
from typing import List, Optional
from uuid import uuid4
from datetime import datetime
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
from fastapi import HTTPException, Request, status
from src.db.api_tokens import APIToken, APITokenCreate, APITokenCreatedResponse, APITokenRead, APITokenUpdate
from src.db.organizations import Organization
from src.db.users import PublicUser, AnonymousUser, APITokenUser
from src.db.roles import Rights
from src.security.rbac.rbac import authorization_verify_if_user_is_anon
from src.security.org_auth import is_org_member, require_org_role_permission, get_user_org_role
from src.security.superadmin import is_user_superadmin
from src.security.security import security_hash_token, security_token_needs_rehash, security_verify_token
TOKEN_PREFIX = 'lh_'
TOKEN_BYTES = 32

def generate_api_token() -> tuple[str, str, str]:
    """
    Generate a new API token.

    Returns:
        tuple: (full_token, token_prefix, token_hash)
        - full_token: The complete token to return to the user (only shown once!)
        - token_prefix: First 12 characters of the token for display
        - token_hash: SHA-256 hash of the token for storage
    """
    random_part = secrets.token_urlsafe(TOKEN_BYTES)
    full_token = f'{TOKEN_PREFIX}{random_part}'
    token_prefix = full_token[:12]
    token_hash = security_hash_token(full_token)
    return (full_token, token_prefix, token_hash)

def hash_token(token: str) -> str:
    return security_hash_token(token)

def _block_api_tokens(current_user) -> None:
    """Block API tokens from managing other API tokens.

    SECURITY: Allowing a token to create / read / update / revoke other tokens
    is a privilege-escalation surface: a low-rights token could spawn a
    higher-rights one, or revoke the admin's active token. Token management
    is a human/admin action and must stay behind user authentication.
    """
    if isinstance(current_user, APITokenUser):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='API tokens cannot manage other API tokens. Use user authentication.')

def _parse_expires_at(value: str) -> datetime:
    return datetime.fromisoformat(value.replace('Z', '+00:00'))

def _validate_expires_at(value: Optional[str]) -> None:
    """400 unless ``expires_at`` is empty or an ISO 8601 datetime.

    Authentication refuses a token whose expiry cannot be parsed, so a bad
    value must be caught here rather than minting a token that never works.
    """
    if not value:
        return
    try:
        _parse_expires_at(value)
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='expires_at must be an ISO 8601 datetime')

def verify_token(provided_token: str, stored_hash: str) -> bool:
    """
    Verify a token against its stored hash using timing-safe comparison.

    Args:
        provided_token: The token provided in the request
        stored_hash: The hash stored in the database

    Returns:
        bool: True if the token is valid
    """
    return security_verify_token(provided_token, stored_hash)

async def create_api_token(request: Request, db_session: AsyncSession, token_data: APITokenCreate, org_id: int, current_user: PublicUser | AnonymousUser | APITokenUser) -> APITokenCreatedResponse:
    """
    Create a new API token for an organization.

    Args:
        request: FastAPI request object
        db_session: Database session
        token_data: Token creation data
        org_id: Organization ID
        current_user: The authenticated user creating the token

    Returns:
        APITokenCreatedResponse: The created token with the full token value (only time it's shown!)
    """
    _block_api_tokens(current_user)
    await authorization_verify_if_user_is_anon(current_user.id)
    statement = select(Organization).where(Organization.id == org_id)
    organization = (await db_session.execute(statement)).scalars().first()
    if not organization:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Organization not found')
    await require_org_role_permission(current_user.id, org_id, db_session, 'roles', 'action_create')
    from src.services.demo.guards import require_not_demo_org
    await require_not_demo_org(org_id, db_session)
    if not token_data.name or token_data.name.strip() == '':
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Token name is required and cannot be empty')
    if len(token_data.name.strip()) > 100:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Token name cannot exceed 100 characters')
    statement = select(APIToken).where(APIToken.name == token_data.name.strip(), APIToken.org_id == org_id, APIToken.is_active == True)
    existing_token = (await db_session.execute(statement)).scalars().first()
    if existing_token:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"An active API token with the name '{token_data.name}' already exists in this organization")
    _validate_expires_at(token_data.expires_at)
    if token_data.rights:
        user_role = await get_user_org_role(current_user.id, org_id, db_session)
        user_rights = user_role.rights if user_role else None
        await validate_rights_structure(token_data.rights, user_rights)
    full_token, token_prefix, token_hash = generate_api_token()
    now = str(datetime.now())
    api_token = APIToken(token_uuid=f'apitoken_{uuid4()}', name=token_data.name.strip(), description=token_data.description, token_prefix=token_prefix, token_hash=token_hash, org_id=org_id, rights=token_data.rights.model_dump() if isinstance(token_data.rights, Rights) else token_data.rights, created_by_user_id=current_user.id, creation_date=now, update_date=now, expires_at=token_data.expires_at, is_active=True)
    db_session.add(api_token)
    await db_session.commit()
    await db_session.refresh(api_token)
    return APITokenCreatedResponse(token=full_token, token_uuid=api_token.token_uuid, name=api_token.name, description=api_token.description, token_prefix=api_token.token_prefix, org_id=api_token.org_id, rights=api_token.rights, created_by_user_id=api_token.created_by_user_id, creation_date=api_token.creation_date, expires_at=api_token.expires_at)

async def list_api_tokens(request: Request, db_session: AsyncSession, org_id: int, current_user: PublicUser | AnonymousUser | APITokenUser) -> List[APITokenRead]:
    """List all API tokens for an organization."""
    _block_api_tokens(current_user)
    await authorization_verify_if_user_is_anon(current_user.id)
    statement = select(Organization).where(Organization.id == org_id)
    organization = (await db_session.execute(statement)).scalars().first()
    if not organization:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='Organization not found')
    await require_org_role_permission(current_user.id, org_id, db_session, 'roles', 'action_read')
    statement = select(APIToken).where(APIToken.org_id == org_id).order_by(APIToken.creation_date.desc())
    tokens = (await db_session.execute(statement)).scalars().all()
    return [APITokenRead(**token.model_dump()) for token in tokens]

async def get_api_token(request: Request, db_session: AsyncSession, org_id: int, token_uuid: str, current_user: PublicUser | AnonymousUser | APITokenUser) -> APITokenRead:
    """Get a specific API token by UUID."""
    _block_api_tokens(current_user)
    await authorization_verify_if_user_is_anon(current_user.id)
    await require_org_role_permission(current_user.id, org_id, db_session, 'roles', 'action_read')
    statement = select(APIToken).where(APIToken.token_uuid == token_uuid, APIToken.org_id == org_id)
    token = (await db_session.execute(statement)).scalars().first()
    if not token:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='API token not found')
    return APITokenRead(**token.model_dump())

async def update_api_token(request: Request, db_session: AsyncSession, org_id: int, token_uuid: str, token_data: APITokenUpdate, current_user: PublicUser | AnonymousUser | APITokenUser) -> APITokenRead:
    """Update an API token."""
    _block_api_tokens(current_user)
    await authorization_verify_if_user_is_anon(current_user.id)
    await require_org_role_permission(current_user.id, org_id, db_session, 'roles', 'action_update')
    statement = select(APIToken).where(APIToken.token_uuid == token_uuid, APIToken.org_id == org_id)
    token = (await db_session.execute(statement)).scalars().first()
    if not token:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='API token not found')
    _validate_expires_at(token_data.expires_at)
    if token_data.rights:
        user_role = await get_user_org_role(current_user.id, org_id, db_session)
        user_rights = user_role.rights if user_role else None
        await validate_rights_structure(token_data.rights, user_rights)
    update_data = token_data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        if value is not None:
            if key == 'rights' and isinstance(value, Rights):
                value = value.model_dump()
            setattr(token, key, value)
    token.update_date = str(datetime.now())
    db_session.add(token)
    await db_session.commit()
    await db_session.refresh(token)
    return APITokenRead(**token.model_dump())

async def revoke_api_token(request: Request, db_session: AsyncSession, org_id: int, token_uuid: str, current_user: PublicUser | AnonymousUser | APITokenUser) -> dict:
    """Revoke (soft delete) an API token."""
    _block_api_tokens(current_user)
    await authorization_verify_if_user_is_anon(current_user.id)
    await require_org_role_permission(current_user.id, org_id, db_session, 'roles', 'action_delete')
    statement = select(APIToken).where(APIToken.token_uuid == token_uuid, APIToken.org_id == org_id)
    token = (await db_session.execute(statement)).scalars().first()
    if not token:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='API token not found')
    token.is_active = False
    token.update_date = str(datetime.now())
    db_session.add(token)
    await db_session.commit()
    return {'message': 'API token revoked successfully'}

async def regenerate_api_token(request: Request, db_session: AsyncSession, org_id: int, token_uuid: str, current_user: PublicUser | AnonymousUser | APITokenUser) -> APITokenCreatedResponse:
    """
    Regenerate the secret for an API token.
    The old token will no longer work.
    """
    _block_api_tokens(current_user)
    await authorization_verify_if_user_is_anon(current_user.id)
    await require_org_role_permission(current_user.id, org_id, db_session, 'roles', 'action_update')
    statement = select(APIToken).where(APIToken.token_uuid == token_uuid, APIToken.org_id == org_id)
    token = (await db_session.execute(statement)).scalars().first()
    if not token:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail='API token not found')
    if not token.is_active:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='Cannot regenerate a revoked token')
    if token.created_by_user_id != current_user.id and (not await is_user_superadmin(current_user.id, db_session)):
        caller_role = await get_user_org_role(current_user.id, org_id, db_session)
        if cap_token_rights(token.rights, caller_role) != _rights_dict(token.rights):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='You can only regenerate tokens you created or whose rights you hold yourself')
    full_token, token_prefix, token_hash = generate_api_token()
    token.token_prefix = token_prefix
    token.token_hash = token_hash
    token.update_date = str(datetime.now())
    db_session.add(token)
    await db_session.commit()
    await db_session.refresh(token)
    return APITokenCreatedResponse(token=full_token, token_uuid=token.token_uuid, name=token.name, description=token.description, token_prefix=token.token_prefix, org_id=token.org_id, rights=token.rights, created_by_user_id=token.created_by_user_id, creation_date=token.creation_date, expires_at=token.expires_at)

async def _require_api_access_plan(org_id: int, db_session: AsyncSession) -> None:
    """403 unless ``org_id``'s plan still includes API access.

    Same check (and same plan floor) as the token-management routes; self-
    hosted modes bypass it there and here alike.
    """
    from src.security.features_utils.plan_check import check_org_plan
    from src.security.features_utils.plans import FEATURE_PLAN_REQUIREMENTS
    try:
        await check_org_plan(org_id, FEATURE_PLAN_REQUIREMENTS['api_tokens'], 'API Access', db_session)
    except HTTPException as e:
        if e.status_code == status.HTTP_403_FORBIDDEN:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f'API token rejected: {e.detail}')
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="API token rejected: the organization's plan does not include API access.")

async def _api_token_is_live(api_token: APIToken, db_session: AsyncSession) -> bool:
    """Whether an active token may still act: not expired, plan still includes
    API access (403 otherwise), creator still a member of the org."""
    if api_token.expires_at:
        try:
            expires_at = _parse_expires_at(api_token.expires_at)
            now = datetime.now(expires_at.tzinfo)
        except (ValueError, TypeError, AttributeError):
            return False
        if now > expires_at:
            return False
    await _require_api_access_plan(api_token.org_id, db_session)
    return await is_org_member(api_token.created_by_user_id, api_token.org_id, db_session)

def _rights_dict(rights) -> dict:
    """Token rights as a plain ``{bucket: {perm: value}}`` dict."""
    if not rights:
        return {}
    if isinstance(rights, Rights):
        return rights.model_dump()
    return {bucket: perms.model_dump() if hasattr(perms, 'model_dump') else perms for bucket, perms in rights.items()}

def cap_token_rights(token_rights, role) -> dict:
    """``token_rights`` limited to what ``role`` grants right now.

    Rights are capped against the creator's role when a token is created or
    edited, but roles change afterwards: a demoted creator's token must not
    keep the rights it was minted with. A permission survives only when the
    token grants it as a boolean ``True`` and the role holds it too (token-only
    buckets are capped by the closest role bucket, as at creation). A role with
    no rights object is judged like ``require_org_role_permission`` judges it:
    Admin/Maintainer hold everything, any other role nothing.
    """
    from src.security.rbac.constants import ADMIN_OR_MAINTAINER_ROLE_IDS
    role_rights = _rights_dict(getattr(role, 'rights', None))
    unrestricted = role is not None and (not role_rights) and (role.id in ADMIN_OR_MAINTAINER_ROLE_IDS)
    capped: dict = {}
    for bucket, perms in _rights_dict(token_rights).items():
        if not isinstance(perms, dict):
            capped[bucket] = {}
            continue
        cap_key = bucket if bucket in role_rights else _TOKEN_BUCKET_CAPS.get(bucket)
        role_perms = role_rights.get(cap_key) if cap_key else None
        capped[bucket] = {perm: value is True and (unrestricted or (isinstance(role_perms, dict) and bool(role_perms.get(perm)))) for perm, value in perms.items()}
    return capped

async def _with_creator_capped_rights(api_token: APIToken, db_session: AsyncSession) -> APIToken:
    """The token as it may act now: its rights capped by its creator's current
    role in the org. The stored row is never modified; when the cap removes
    anything, a detached copy carrying the capped rights is returned instead."""
    rights = getattr(api_token, 'rights', None)
    if not rights:
        return api_token
    role = await get_user_org_role(api_token.created_by_user_id, api_token.org_id, db_session)
    capped = cap_token_rights(rights, role)
    if capped == _rights_dict(rights):
        return api_token
    return APIToken(**{**api_token.model_dump(), 'rights': capped})

async def get_live_api_token(token_id: int, db_session: AsyncSession) -> Optional[APIToken]:
    """The token with ``token_id`` if it could still authenticate a request now.

    For credentials a token issued earlier (a sign-in link) that must stop
    working once the token is revoked, expires or loses its plan.
    """
    api_token = (await db_session.execute(select(APIToken).where(APIToken.id == token_id, APIToken.is_active == True))).scalars().first()
    if api_token is None:
        return None
    try:
        if not await _api_token_is_live(api_token, db_session):
            return None
    except HTTPException:
        return None
    return await _with_creator_capped_rights(api_token, db_session)

async def validate_api_token_for_auth(token: str, db_session: AsyncSession) -> Optional[APIToken]:
    """
    Validate an API token for authentication purposes.
    Updates last_used_at on successful validation.

    Args:
        token: The full token string (lh_...)
        db_session: Database session

    Returns:
        APIToken if valid, None otherwise
    """
    if not token.startswith(TOKEN_PREFIX):
        return None
    statement = select(APIToken).where(APIToken.token_prefix == token[:12], APIToken.is_active == True)
    candidates = (await db_session.execute(statement)).scalars().all()
    api_token = next((t for t in candidates if security_verify_token(token, t.token_hash)), None)
    if not api_token:
        return None
    if not await _api_token_is_live(api_token, db_session):
        return None
    try:
        api_token.last_used_at = str(datetime.now())
        if security_token_needs_rehash(api_token.token_hash):
            api_token.token_hash = security_hash_token(token)
        db_session.add(api_token)
        await db_session.commit()
        await db_session.refresh(api_token)
    except Exception:
        pass
    return await _with_creator_capped_rights(api_token, db_session)
_TOKEN_BUCKET_CAPS = {'certifications': 'courses', 'search': 'courses'}

async def validate_rights_structure(rights: Optional[Rights | dict], user_rights: Optional[Rights | dict]) -> None:
    """
    Validate the rights structure and ensure token rights don't exceed user rights.

    Args:
        rights: The rights to validate
        user_rights: The creating user's rights
    """
    if not rights:
        return
    if isinstance(rights, Rights):
        rights_dict = rights.model_dump()
    else:
        rights_dict = rights
    required_rights = ['courses', 'activities', 'coursechapters', 'certifications', 'usergroups', 'search']
    for required_right in required_rights:
        if required_right not in rights_dict:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f'Missing required right: {required_right}')
        if not isinstance(rights_dict[required_right], dict):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Right '{required_right}' must be a JSON object")
    for right_key, right_data in rights_dict.items():
        if not isinstance(right_data, dict):
            continue
        for perm_key, perm_value in right_data.items():
            if not isinstance(perm_value, bool):
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Permission '{perm_key}' for '{right_key}' must be a boolean")
    if user_rights:
        user_rights_dict = user_rights.model_dump() if isinstance(user_rights, Rights) else user_rights
        for right_key, right_permissions in rights_dict.items():
            cap_key = right_key if right_key in user_rights_dict else _TOKEN_BUCKET_CAPS.get(right_key)
            user_right_permissions = user_rights_dict.get(cap_key) if cap_key else None
            if not isinstance(right_permissions, dict):
                continue
            for perm_key, perm_value in right_permissions.items():
                if perm_value:
                    user_has_perm = isinstance(user_right_permissions, dict) and bool(user_right_permissions.get(perm_key))
                    if not user_has_perm:
                        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"Cannot grant '{perm_key}' permission for '{right_key}' as you don't have this permission yourself")
