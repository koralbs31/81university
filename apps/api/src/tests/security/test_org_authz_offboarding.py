"""Org-level authorization, session policy and offboarding.

* Security-sensitive org settings (signup mechanism, staff removal, custom
  domains, webhooks) take the real organizations/users right, not just the
  Admin-or-Maintainer role id rbac_check accepts.
* Org session policies are enforced by rbac_check and the analytics gates.
* Removing or leaving an org revokes the per-resource grants a member held.
* Custom domains and webhooks stop once the plan no longer includes them.
"""
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from fastapi import HTTPException
from src.db.custom_domains import CustomDomain, CustomDomainCreate
from src.db.organization_config import OrganizationConfig
from src.db.organizations import OrganizationCreate, OrganizationUpdate
from src.db.resource_authors import ResourceAuthor, ResourceAuthorshipEnum, ResourceAuthorshipStatusEnum
from src.db.users import PublicUser, User
from src.db.webhooks import WebhookEndpointCreate
from src.tests.security import test_org_admin_gates as _gates
_make_member = _gates._make_member
maintainer_role = _gates.maintainer_role
maintainer_user = _gates.maintainer_user
side_effects = _gates.side_effects
NOW = str(datetime.now())

async def _org_config(db, org, plan='free'):
    db.add(OrganizationConfig(org_id=org.id, config={'config_version': '2.0', 'plan': plan, 'admin_toggles': {}}))
    await db.commit()

def _saas():
    """Patch both bindings of get_deployment_mode used by plan checks."""
    from contextlib import ExitStack
    stack = ExitStack()
    stack.enter_context(patch('src.core.deployment_mode.get_deployment_mode', return_value='saas'))
    stack.enter_context(patch('src.security.features_utils.plan_check.get_deployment_mode', return_value='saas'))
    return stack

async def _outsider(db, user_id=9, username='outsider'):
    u = User(id=user_id, username=username, first_name=username, last_name='X', email=f'{username}@test.com', password='x', user_uuid=f'user_{username}', creation_date=NOW, update_date=NOW)
    db.add(u)
    await db.commit()
    return PublicUser(id=u.id, username=u.username, first_name=u.first_name, last_name=u.last_name, email=u.email, user_uuid=u.user_uuid)

class TestSensitiveOrgSettings:

    async def test_maintainer_cannot_change_signup_mechanism(self, db, org, admin_user, maintainer_user, mock_request):
        from src.services.orgs.orgs import update_org_signup_mechanism
        await _org_config(db, org)
        with pytest.raises(HTTPException) as exc:
            await update_org_signup_mechanism(mock_request, 'open', org.id, maintainer_user, db)
        assert exc.value.status_code == 403

    async def test_admin_can_change_signup_mechanism(self, db, org, admin_user, mock_request):
        from src.services.orgs.orgs import update_org_signup_mechanism
        await _org_config(db, org)
        with patch('src.services.orgs.orgs.dispatch_webhooks', new_callable=AsyncMock):
            result = await update_org_signup_mechanism(mock_request, 'open', org.id, admin_user, db)
        assert result['detail'] == 'Signup mechanism updated'

    async def test_maintainer_cannot_remove_maintainer(self, db, org, admin_user, maintainer_user, mock_request, side_effects):
        from src.services.orgs.users import remove_user_from_org
        other = await _make_member(db, org, 6, 'maintainer2', 2)
        with pytest.raises(HTTPException) as exc:
            await remove_user_from_org(mock_request, org.id, other.id, db, maintainer_user)
        assert exc.value.status_code == 403

    async def test_maintainer_cannot_batch_remove_maintainer(self, db, org, admin_user, maintainer_user, mock_request, side_effects):
        from src.services.orgs.users import remove_batch_users_from_org
        other = await _make_member(db, org, 6, 'maintainer2', 2)
        with pytest.raises(HTTPException) as exc:
            await remove_batch_users_from_org(mock_request, org.id, [other.id], db, maintainer_user)
        assert exc.value.status_code == 403

    async def test_admin_can_remove_maintainer(self, db, org, admin_user, maintainer_user, mock_request, side_effects):
        from src.services.orgs.users import remove_user_from_org
        result = await remove_user_from_org(mock_request, org.id, maintainer_user.id, db, admin_user)
        assert result['detail'] == 'User removed from org'

    async def test_maintainer_cannot_manage_custom_domains(self, db, org, admin_user, maintainer_user, mock_request):
        from src.services.orgs.custom_domains import add_custom_domain, delete_custom_domain, verify_custom_domain
        db.add(CustomDomain(domain_uuid='domain_1', domain='learn.example.com', org_id=org.id, status='verified', creation_date=NOW, update_date=NOW))
        await db.commit()
        for call in (add_custom_domain(mock_request, db, CustomDomainCreate(domain='new.example.com'), org.id, maintainer_user), verify_custom_domain(mock_request, db, org.id, 'domain_1', maintainer_user), delete_custom_domain(mock_request, db, org.id, 'domain_1', maintainer_user)):
            with pytest.raises(HTTPException) as exc:
                await call
            assert exc.value.status_code == 403

    async def test_maintainer_cannot_create_webhook(self, db, org, admin_user, maintainer_user, mock_request):
        from src.services.webhooks.webhooks import create_webhook_endpoint
        with pytest.raises(HTTPException) as exc:
            await create_webhook_endpoint(mock_request, db, org.id, WebhookEndpointCreate(url='https://example.com/h', events=['course_created']), maintainer_user)
        assert exc.value.status_code == 403

class TestOrgSessionPolicy:

    async def test_rbac_check_enforces_org_policy(self, db, org, maintainer_user, mock_request):
        from src.services.orgs.orgs import rbac_check
        with patch('src.services.orgs.orgs.enforce_org_mfa', new_callable=AsyncMock) as mfa:
            await rbac_check(mock_request, org.org_uuid, maintainer_user, 'update', db)
        mfa.assert_awaited_once_with(maintainer_user.id, org.id, db)

    async def test_rbac_check_read_stays_public(self, db, org, regular_user, mock_request):
        from src.services.orgs.orgs import rbac_check
        with patch('src.services.orgs.orgs.enforce_org_mfa', new_callable=AsyncMock) as mfa:
            assert await rbac_check(mock_request, org.org_uuid, regular_user, 'read', db)
        mfa.assert_not_awaited()

    async def test_analytics_gates_enforce_org_policy(self, db, org, admin_user):
        from src.security.org_auth import verify_org_admin, verify_org_membership
        blocked = HTTPException(status_code=403, detail='2fa')
        with patch('src.security.org_auth.enforce_org_mfa', new_callable=AsyncMock, side_effect=blocked):
            with pytest.raises(HTTPException):
                await verify_org_membership(admin_user.id, org.id, db)
            with pytest.raises(HTTPException):
                await verify_org_admin(admin_user.id, org.id, db)

    async def test_superadmin_is_not_blocked_by_policy(self, db, org):
        from src.security.org_auth import verify_org_membership
        boss = await _outsider(db, 20, 'boss')
        with patch('src.security.org_auth.is_user_superadmin', new_callable=AsyncMock, return_value=True), patch('src.security.org_auth.enforce_org_mfa', new_callable=AsyncMock) as mfa:
            await verify_org_membership(boss.id, org.id, db)
        mfa.assert_not_awaited()

class TestOffboarding:
    pass

    async def test_invite_cleanup_failure_does_not_block_removal(self, db, org, regular_user):
        from src.db.organizations import Organization
        from src.services.orgs.users import _drop_accepted_invites
        fake_redis = MagicMock()
        fake_redis.delete.side_effect = RuntimeError('redis down')
        org_row = await db.get(Organization, org.id)
        with patch('src.core.redis.get_redis_client', return_value=fake_redis), patch('src.services.orgs.users.logger') as log:
            await _drop_accepted_invites(db, org_row, [regular_user.id])
        fake_redis.delete.assert_called_once_with(f'invited_user:{regular_user.email}:org:{org.org_uuid}')
        assert log.warning.call_count == 1

class TestStaleGrantsIgnored:

    async def test_author_row_needs_org_membership(self, db, org, course, mock_request):
        from src.security.rbac.resource_access import ResourceAccessChecker
        outsider = await _outsider(db)
        db.add(ResourceAuthor(resource_uuid=course.course_uuid, user_id=outsider.id, authorship=ResourceAuthorshipEnum.CREATOR, authorship_status=ResourceAuthorshipStatusEnum.ACTIVE))
        await db.commit()
        checker = ResourceAccessChecker(mock_request, db, outsider)
        assert await checker._is_resource_author(course.course_uuid) is False

    async def test_author_member_still_author(self, db, org, course, regular_user, mock_request):
        from src.security.rbac.resource_access import ResourceAccessChecker
        db.add(ResourceAuthor(resource_uuid=course.course_uuid, user_id=regular_user.id, authorship=ResourceAuthorshipEnum.CREATOR, authorship_status=ResourceAuthorshipStatusEnum.ACTIVE))
        await db.commit()
        checker = ResourceAccessChecker(mock_request, db, regular_user)
        assert await checker._is_resource_author(course.course_uuid) is True
    pass
    pass

class TestOrgListEmail:

    async def test_member_does_not_see_org_email(self, db, org, regular_user, mock_request):
        from src.services.orgs.orgs import get_orgs_by_user
        orgs = await get_orgs_by_user(mock_request, db, regular_user.id)
        assert orgs[0].email is None

    async def test_admin_sees_org_email(self, db, org, admin_user, mock_request):
        from src.services.orgs.orgs import get_orgs_by_user, get_orgs_by_user_admin
        assert (await get_orgs_by_user(mock_request, db, admin_user.id))[0].email == org.email
        assert (await get_orgs_by_user_admin(mock_request, db, admin_user.id))[0].email == org.email

    async def test_superadmin_sees_org_email(self, db, org, regular_user, mock_request):
        from src.services.orgs.orgs import get_orgs_by_user
        user = await db.get(User, regular_user.id)
        user.is_superadmin = True
        db.add(user)
        await db.commit()
        assert (await get_orgs_by_user(mock_request, db, regular_user.id))[0].email == org.email

    async def test_builtin_admin_role_without_stored_rights_sees_org_email(self, db, org, admin_role, admin_user, mock_request):
        from src.services.orgs.orgs import get_orgs_by_user
        admin_role.rights = None
        db.add(admin_role)
        await db.commit()
        assert (await get_orgs_by_user(mock_request, db, admin_user.id))[0].email == org.email

    async def test_user_without_orgs_gets_empty_list(self, db, org, mock_request):
        from src.services.orgs.orgs import get_orgs_by_user
        outsider = await _outsider(db)
        assert await get_orgs_by_user(mock_request, db, outsider.id) == []

class TestPlanGatedServing:

    async def _domain(self, db, org):
        db.add(CustomDomain(domain_uuid='domain_1', domain='learn.example.com', org_id=org.id, status='verified', creation_date=NOW, update_date=NOW))
        await db.commit()

    async def test_domain_not_served_after_downgrade(self, db, org):
        from src.services.orgs.custom_domains import resolve_org_by_domain
        await self._domain(db, org)
        await _org_config(db, org, plan='free')
        with _saas():
            assert await resolve_org_by_domain(db, 'learn.example.com') is None

    async def test_domain_served_on_entitled_plan(self, db, org):
        from src.services.orgs.custom_domains import resolve_org_by_domain
        await self._domain(db, org)
        await _org_config(db, org, plan='standard')
        with _saas():
            result = await resolve_org_by_domain(db, 'learn.example.com')
        assert result.org_id == org.id

    async def test_domain_served_when_plan_lookup_errors(self, db, org):
        from src.services.orgs.custom_domains import resolve_org_by_domain
        await self._domain(db, org)
        with _saas(), patch('src.security.features_utils.plan_check.get_org_plan', new_callable=AsyncMock, side_effect=RuntimeError('db down')):
            result = await resolve_org_by_domain(db, 'learn.example.com')
        assert result.org_id == org.id

    async def test_webhooks_need_plan(self, db, org, other_org):
        from src.services.webhooks.dispatch import _org_plan_includes_webhooks
        await _org_config(db, org, plan='standard')
        await _org_config(db, other_org, plan='pro')
        with _saas():
            assert await _org_plan_includes_webhooks(org.id, db) is False
            assert await _org_plan_includes_webhooks(other_org.id, db) is True

class TestSlugFormat:

    @pytest.mark.parametrize('slug', ['evil.com/x', 'a?b', 'a"b', 'a#b', 'a b', '-', ''])
    async def test_create_rejects_bad_slug(self, db, org, admin_user, mock_request, slug):
        from src.services.orgs.orgs import create_org
        with patch('src.services.orgs.orgs.is_multi_org_allowed', return_value=True):
            with pytest.raises(HTTPException) as exc:
                await create_org(mock_request, OrganizationCreate(name='N', slug=slug, email='a@b.com'), admin_user, db)
        assert exc.value.status_code == 400

    def test_normalizes_case_and_edges(self):
        from src.services.orgs.orgs import normalize_org_slug
        assert normalize_org_slug(' My-Org- ') == 'my-org'

    async def test_update_validates_changed_slug_only(self, db, org, admin_user, mock_request):
        from src.services.orgs.orgs import update_org
        org.slug = 'Legacy_Slug'
        db.add(org)
        await db.commit()
        result = await update_org(mock_request, OrganizationUpdate(name='R', slug='Legacy_Slug'), org.id, admin_user, db)
        assert result.slug == 'Legacy_Slug'
        with pytest.raises(HTTPException) as exc:
            await update_org(mock_request, OrganizationUpdate(slug='x/y'), org.id, admin_user, db)
        assert exc.value.status_code == 400

class TestCsvAndKeys:

    @pytest.mark.parametrize('value', [' =cmd', '\xa0+1', '  @x', '=1'])
    def test_csv_safe_leading_whitespace(self, value):
        from src.services.orgs.users import _csv_safe
        assert _csv_safe(value).startswith("'")
    pass

    async def test_domains_internal_key_non_ascii(self, db, mock_request, monkeypatch):
        from src.routers.orgs.custom_domains import api_list_all_verified_domains
        monkeypatch.setenv('CLOUD_INTERNAL_KEY', 'secret')
        with pytest.raises(HTTPException) as exc:
            await api_list_all_verified_domains(mock_request, x_internal_key='é', db_session=db)
        assert exc.value.status_code == 403
