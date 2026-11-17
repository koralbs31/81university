"""Wave 3 org service coverage for src/services/orgs/orgs.py."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import pytest
from fastapi import HTTPException
from sqlmodel import select
from src.db.organization_config import OrganizationConfig, OrganizationConfigV2Base
from src.db.organizations import Organization, OrganizationCreate, OrganizationRead
from src.db.user_organizations import UserOrganization
from src.db.users import APITokenUser, InternalUser
from src.services.orgs.orgs import _update_feature_toggle, create_org, create_org_with_config, delete_org, get_org_join_mechanism, get_orgs_by_user, get_orgs_by_user_admin, rbac_check, update_org_favicon, update_org_logo, update_org_preview, update_org_thumbnail, update_org_with_config_no_auth, upload_org_auth_background_service, upload_org_landing_content_service, upload_org_og_image_service, upload_org_preview_service

async def _make_org_config(db, org, config):
    row = OrganizationConfig(org_id=org.id, config=config, creation_date=str(datetime.now()), update_date=str(datetime.now()))
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row

async def _attach_user_to_org(db, user_id, org_id, role_id):
    row = UserOrganization(user_id=user_id, org_id=org_id, role_id=role_id, creation_date=str(datetime.now()), update_date=str(datetime.now()))
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row

class TestOrgCreationAndListingWave3:

    @pytest.mark.asyncio
    @patch('src.services.orgs.orgs.is_multi_org_allowed', return_value=True)
    @patch('src.routers.users._invalidate_session_cache')
    async def test_create_org_with_config_persists_v2_config(self, mock_invalidate, mock_multi_org, mock_request, db, admin_user):
        new_org = OrganizationCreate(name='Wave3 Org', slug='wave3-org', email='wave3@org.com')
        created = await create_org_with_config(mock_request, new_org, admin_user, db, OrganizationConfigV2Base(plan='pro', customization={'landing': {'hero': 'created'}}))
        assert isinstance(created, OrganizationRead)
        assert created.slug == 'wave3-org'
        assert created.config.config['config_version'] == '2.0'
        stored = (await db.execute(select(OrganizationConfig).where(OrganizationConfig.org_id == created.id))).scalars().first()
        assert stored is not None
        assert stored.config['plan'] == 'pro'

    @pytest.mark.asyncio
    @patch('src.services.orgs.orgs.is_multi_org_allowed', return_value=True)
    @patch('src.routers.users._invalidate_session_cache')
    async def test_update_org_with_config_no_auth_updates_existing_config(self, mock_invalidate, mock_multi_org, mock_request, db, admin_user):
        new_org = OrganizationCreate(name='Wave3 Org', slug='wave3-org', email='wave3@org.com')
        created = await create_org_with_config(mock_request, new_org, admin_user, db, OrganizationConfigV2Base(plan='pro', customization={'landing': {'hero': 'created'}}))
        updated = await update_org_with_config_no_auth(mock_request, OrganizationConfigV2Base(plan='enterprise', customization={'landing': {'hero': 'updated'}}), created.id, db)
        assert updated == {'detail': 'Organization updated'}
        stored = (await db.execute(select(OrganizationConfig).where(OrganizationConfig.org_id == created.id))).scalars().first()
        assert stored.config['plan'] == 'enterprise'
        assert stored.config['customization']['landing']['hero'] == 'updated'
        mock_invalidate.assert_called_once_with(admin_user.id)

    @pytest.mark.asyncio
    @patch('src.services.orgs.orgs.is_multi_org_allowed', return_value=True)
    @patch('src.routers.users._invalidate_session_cache')
    async def test_create_org_skips_missing_config_logging(self, mock_invalidate, mock_multi_org, mock_request, db, admin_user):
        original_execute = db.execute

        async def execute_side_effect(statement, *args, **kwargs):
            if 'organizationconfig' in str(statement).lower():
                from unittest.mock import MagicMock
                result = MagicMock()
                result.scalars.return_value.first.return_value = None
                result.scalar_one_or_none.return_value = None
                return result
            return await original_execute(statement, *args, **kwargs)
        with patch.object(db, 'execute', side_effect=execute_side_effect), patch('src.services.orgs.orgs.OrganizationConfig.model_validate', return_value=OrganizationConfig(org_id=0, config={}, creation_date='', update_date='')):
            created = await create_org(mock_request, OrganizationCreate(name='No Config Org', slug='no-config-org', email='noconfig@org.com'), admin_user, db)
        assert created.slug == 'no-config-org'
        assert mock_invalidate.call_count == 1

    @pytest.mark.asyncio
    @patch('src.services.orgs.orgs.is_multi_org_allowed', return_value=True)
    @patch('src.routers.users._invalidate_session_cache')
    async def test_create_org_with_config_skips_missing_config_logging(self, mock_invalidate, mock_multi_org, mock_request, db, admin_user):
        original_execute = db.execute

        async def execute_side_effect(statement, *args, **kwargs):
            if 'organizationconfig' in str(statement).lower():
                from unittest.mock import MagicMock
                result = MagicMock()
                result.scalars.return_value.first.return_value = None
                result.scalar_one_or_none.return_value = None
                return result
            return await original_execute(statement, *args, **kwargs)
        with patch.object(db, 'execute', side_effect=execute_side_effect), patch('src.services.orgs.orgs.OrganizationConfig.model_validate', return_value=OrganizationConfig(org_id=0, config={}, creation_date='', update_date='')):
            created = await create_org_with_config(mock_request, OrganizationCreate(name='No Config Org 2', slug='no-config-org-2', email='noconfig2@org.com'), admin_user, db, {'config_version': '2.0', 'plan': 'free'})
        assert created.slug == 'no-config-org-2'
        assert mock_invalidate.call_count == 1

    @pytest.mark.asyncio
    @patch('src.services.orgs.orgs.is_multi_org_allowed', return_value=True)
    async def test_create_org_with_config_failure_branches(self, mock_multi_org, mock_request, db, org, admin_user, anonymous_user):
        duplicate = OrganizationCreate(name='Duplicate Config Org', slug='test-org', email='dup-config@org.com')
        with pytest.raises(HTTPException) as duplicate_exc:
            await create_org_with_config(mock_request, duplicate, admin_user, db, {'config_version': '2.0', 'plan': 'free'})
        assert duplicate_exc.value.status_code == 409
        unique = OrganizationCreate(name='Anon Config Org', slug='anon-config-org', email='anon-config@org.com')
        with pytest.raises(HTTPException) as anon_exc:
            await create_org_with_config(mock_request, unique, anonymous_user, db, {'config_version': '2.0', 'plan': 'free'})
        assert anon_exc.value.status_code == 409
        with patch('src.services.orgs.orgs.is_multi_org_allowed', return_value=False):
            with pytest.raises(HTTPException) as ee_exc:
                await create_org_with_config(mock_request, OrganizationCreate(name='Blocked Config Org', slug='blocked-config-org', email='blocked-config@org.com'), admin_user, db, {'config_version': '2.0', 'plan': 'free'})
        assert ee_exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_get_org_listing_filters_orgs_by_membership(self, mock_request, db, org, other_org, admin_user, regular_user):
        await _make_org_config(db, org, {'config_version': '2.0', 'plan': 'free', 'admin_toggles': {'members': {'signup_mode': 'open'}}, 'customization': {}})
        await _make_org_config(db, other_org, {'config_version': '1.4', 'cloud': {'plan': 'pro'}, 'features': {'members': {'signup_mode': 'open'}}})
        await _attach_user_to_org(db, admin_user.id, other_org.id, 1)
        admin_orgs = await get_orgs_by_user_admin(mock_request, db, str(admin_user.id), page=1, limit=10)
        assert {item.slug for item in admin_orgs} == {'test-org', 'other-org'}
        regular_orgs = await get_orgs_by_user(mock_request, db, str(regular_user.id), page=1, limit=10)
        assert {item.slug for item in regular_orgs} == {'test-org'}

    @pytest.mark.asyncio
    async def test_get_org_join_mechanism_missing_config_404(self, mock_request, db, admin_user):
        missing_org = Organization(id=99, name='Missing Config Org', slug='missing-config-org', email='missing@org.com', org_uuid='org_missing_config', creation_date=str(datetime.now()), update_date=str(datetime.now()))
        db.add(missing_org)
        await db.commit()
        with patch('src.services.orgs.orgs.rbac_check', new_callable=AsyncMock):
            with pytest.raises(HTTPException) as exc_info:
                await get_org_join_mechanism(mock_request, missing_org.id, admin_user, db)
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_api_token_dict_rights_update_allowed_and_denied(self, mock_request, db, org):
        dict_rights = APITokenUser(org_id=org.id, rights={'organizations': {'action_update': True}}, token_name='dict-update', created_by_user_id=1)
        assert await rbac_check(mock_request, org.org_uuid, dict_rights, 'update', db)
        dict_rights_denied = APITokenUser(org_id=org.id, rights={'organizations': {'action_update': False}}, token_name='dict-denied', created_by_user_id=1)
        with pytest.raises(HTTPException) as denied_exc:
            await rbac_check(mock_request, org.org_uuid, dict_rights_denied, 'update', db)
        assert denied_exc.value.status_code == 403

class TestOrgUploadAndDeleteWave3:

    @pytest.mark.asyncio
    async def test_upload_helpers_and_delete_org(self, mock_request, db, org, admin_user):
        await _make_org_config(db, org, {'config_version': '1.4', 'cloud': {'plan': 'pro'}})
        logo_file = SimpleNamespace(filename='logo.png')
        favicon_file = SimpleNamespace(filename='favicon.png')
        thumbnail_file = SimpleNamespace(filename='thumbnail.png')
        preview_file = SimpleNamespace(filename='preview.png')
        background_file = SimpleNamespace(filename='background.png')
        landing_content_file = SimpleNamespace(filename='landing.txt')
        og_image_file = SimpleNamespace(filename='og.png')
        with patch('src.services.orgs.orgs.rbac_check', new_callable=AsyncMock, return_value=True), patch('src.services.orgs.orgs.upload_org_logo', new_callable=AsyncMock, return_value='stored-logo.png'), patch('src.services.orgs.orgs.upload_org_favicon', new_callable=AsyncMock, return_value='stored-favicon.png'), patch('src.services.orgs.orgs.upload_org_thumbnail', new_callable=AsyncMock, return_value='stored-thumbnail.png'), patch('src.services.orgs.orgs.upload_org_preview', new_callable=AsyncMock, side_effect=['stored-preview.png', 'service-preview.png']), patch('src.services.orgs.orgs.upload_org_auth_background', new_callable=AsyncMock, return_value='stored-background.png'), patch('src.services.orgs.orgs.upload_org_landing_content', new_callable=AsyncMock, return_value='stored-landing-content.txt'), patch('src.services.orgs.orgs.upload_org_og_image', new_callable=AsyncMock, return_value='stored-og.png'), patch('src.routers.users._invalidate_session_cache') as mock_invalidate:
            favicon_result = await update_org_favicon(mock_request, favicon_file, org.id, admin_user, db)
            logo_result = await update_org_logo(mock_request, logo_file, org.id, admin_user, db)
            thumbnail_result = await update_org_thumbnail(mock_request, thumbnail_file, org.id, admin_user, db)
            preview_result = await update_org_preview(mock_request, preview_file, org.id, admin_user, db)
            preview_service_result = await upload_org_preview_service(preview_file, org.org_uuid)
            auth_background_result = await upload_org_auth_background_service(mock_request, background_file, org.id, admin_user, db)
            landing_content_result = await upload_org_landing_content_service(mock_request, landing_content_file, org.id, admin_user, db)
            og_image_result = await upload_org_og_image_service(mock_request, og_image_file, org.id, admin_user, db)
            delete_result = await delete_org(mock_request, org.id, admin_user, db)
        assert favicon_result == {'detail': 'Favicon updated'}
        assert logo_result == {'detail': 'Logo updated'}
        assert thumbnail_result == {'detail': 'Thumbnail updated'}
        assert preview_result == {'name_in_disk': 'stored-preview.png'}
        assert preview_service_result == {'detail': 'Preview uploaded successfully', 'filename': 'service-preview.png'}
        assert auth_background_result == {'detail': 'Auth background uploaded successfully', 'filename': 'stored-background.png'}
        assert landing_content_result == {'detail': 'Landing content uploaded successfully', 'filename': 'stored-landing-content.txt'}
        assert og_image_result == {'detail': 'OG image uploaded successfully', 'filename': 'stored-og.png'}
        assert delete_result['detail'] == 'Organization deleted'
        assert (await db.execute(select(Organization).where(Organization.id == org.id))).scalars().first() is None
        mock_invalidate.assert_called_once_with(admin_user.id)

class TestOrgRbacWave3:

    @pytest.mark.asyncio
    async def test_rbac_check_internal_and_api_token_paths(self, mock_request, db, org):
        internal = InternalUser()
        assert await rbac_check(mock_request, org.org_uuid, internal, 'update', db)
        token_delete = APITokenUser(org_id=org.id, rights={'organizations': {'action_delete': True}}, token_name='delete', created_by_user_id=1)
        with pytest.raises(HTTPException) as delete_exc:
            await rbac_check(mock_request, org.org_uuid, token_delete, 'delete', db)
        assert delete_exc.value.status_code == 403
        token_wrong_org = APITokenUser(org_id=999, rights={'organizations': {'action_update': True}}, token_name='wrong', created_by_user_id=1)
        with pytest.raises(HTTPException) as org_exc:
            await rbac_check(mock_request, org.org_uuid, token_wrong_org, 'update', db)
        assert org_exc.value.status_code == 403
        token_no_rights = APITokenUser(org_id=org.id, rights=None, token_name='empty', created_by_user_id=1)
        with pytest.raises(HTTPException) as rights_exc:
            await rbac_check(mock_request, org.org_uuid, token_no_rights, 'update', db)
        assert rights_exc.value.status_code == 403
        token_object_rights = APITokenUser(org_id=org.id, token_name='object', created_by_user_id=1)
        token_object_rights.rights = SimpleNamespace(organizations=SimpleNamespace(action_update=True))
        assert await rbac_check(mock_request, org.org_uuid, token_object_rights, 'update', db)

    @pytest.mark.asyncio
    async def test_rbac_check_regular_user_branches(self, mock_request, db, org, admin_user, regular_user):
        with patch('src.services.orgs.orgs.authorization_verify_if_user_is_anon', new_callable=AsyncMock, return_value=True), patch('src.services.orgs.orgs.authorization_verify_based_on_org_admin_status', new_callable=AsyncMock, return_value=False):
            with pytest.raises(HTTPException) as anon_exc:
                await rbac_check(mock_request, org.org_uuid, admin_user, 'update', db)
        assert anon_exc.value.status_code == 401
        with patch('src.services.orgs.orgs.authorization_verify_if_user_is_anon', new_callable=AsyncMock, return_value=False), patch('src.services.orgs.orgs.authorization_verify_based_on_org_admin_status', new_callable=AsyncMock, return_value=False):
            with pytest.raises(HTTPException) as forbidden_exc:
                await rbac_check(mock_request, org.org_uuid, regular_user, 'update', db)
        assert forbidden_exc.value.status_code == 403
        with patch('src.services.orgs.orgs.authorization_verify_if_user_is_anon', new_callable=AsyncMock, return_value=False), patch('src.services.orgs.orgs.authorization_verify_based_on_org_admin_status', new_callable=AsyncMock, return_value=True):
            assert await rbac_check(mock_request, org.org_uuid, admin_user, 'update', db) is None

    @pytest.mark.asyncio
    async def test_update_feature_toggle_helper_v2_branch(self, mock_request, db, org, admin_user):
        await _make_org_config(db, org, {'config_version': '2.0', 'customization': {}})
        with patch('src.services.orgs.orgs.rbac_check', new_callable=AsyncMock, return_value=True):
            result = await _update_feature_toggle(mock_request, 'analytics', True, org.id, admin_user, db, v1_default={'enabled': True})
        stored = (await db.execute(select(OrganizationConfig).where(OrganizationConfig.org_id == org.id))).scalars().first()
        assert result == {'detail': 'Analytics configuration updated'}
        assert stored.config['admin_toggles']['analytics']['org_disabled'] is False
        assert 'disabled' not in stored.config['admin_toggles']['analytics']

    @pytest.mark.asyncio
    async def test_update_org_feature_toggle_writes_org_choice(self, mock_request, db, org, admin_user):
        from src.services.orgs.orgs import update_org_feature_toggle
        await _make_org_config(db, org, {'config_version': '2.0', 'customization': {}, 'admin_toggles': {'live_sessions': {'disabled': True}}})
        with patch('src.services.orgs.orgs.rbac_check', new_callable=AsyncMock, return_value=True):
            await update_org_feature_toggle(mock_request, 'live_sessions', False, org.id, admin_user, db)
        stored = (await db.execute(select(OrganizationConfig).where(OrganizationConfig.org_id == org.id))).scalars().first()
        assert stored.config['admin_toggles']['live_sessions'] == {'disabled': True, 'org_disabled': True}

    @pytest.mark.asyncio
    async def test_update_org_feature_toggle_rejects_unknown_feature(self, mock_request, db, org, admin_user):
        from src.services.orgs.orgs import update_org_feature_toggle
        with pytest.raises(HTTPException) as exc_info:
            await update_org_feature_toggle(mock_request, 'sso', False, org.id, admin_user, db)
        assert exc_info.value.status_code == 400

class TestUpdateOrgWithConfigNoAuthMissingOrg:

    @pytest.mark.asyncio
    async def test_update_org_with_config_no_auth_missing_org_raises_404(self, mock_request, db):
        from src.services.orgs.orgs import update_org_with_config_no_auth
        import pytest
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            await update_org_with_config_no_auth(mock_request, {'config_version': '2.0'}, 99999, db)
        assert exc_info.value.status_code == 404
