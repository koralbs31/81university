"""Tests for src/services/users/usergroups.py."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from fastapi import HTTPException
from src.db.users import APITokenUser, InternalUser
from src.db.usergroups import UserGroupCreate, UserGroupUpdate
from src.services.users.usergroups import _validate_resource_exists_and_belongs_to_org, add_resources_to_usergroup, add_users_to_usergroup, create_usergroup, delete_usergroup_by_id, get_resources_by_usergroup, get_usergroups_by_resource, get_users_linked_to_usergroup, read_usergroup_by_id, read_usergroups_by_org_id, remove_resources_from_usergroup, remove_users_from_usergroup, rbac_check, update_usergroup_by_id

class TestUsergroupsService:

    @pytest.mark.asyncio
    async def test_rbac_check_branches(self, mock_request, db, org, admin_user):
        with patch('src.services.users.usergroups.authorization_verify_based_on_roles_and_authorship_or_api_token', new_callable=AsyncMock) as api_token_auth, patch('src.services.users.usergroups.authorization_verify_if_user_is_anon', new_callable=AsyncMock) as anon_auth, patch('src.services.users.usergroups.authorization_verify_based_on_roles_and_authorship', new_callable=AsyncMock) as role_auth, patch('src.services.users.usergroups.require_org_role_permission') as require_perm:
            assert await rbac_check(mock_request, 'usergroup_x', InternalUser(), 'read', db) is True
            token_user = APITokenUser(org_id=org.id, rights={}, token_name='token', created_by_user_id=admin_user.id)
            await rbac_check(mock_request, 'usergroup_x', token_user, 'update', db)
            api_token_auth.assert_awaited_once()
            await rbac_check(mock_request, 'usergroup_x', admin_user, 'delete', db, org_id=org.id)
            anon_auth.assert_awaited_once_with(admin_user.id)
            require_perm.assert_called_once_with(admin_user.id, org.id, db, 'usergroups', 'action_delete')
            await rbac_check(mock_request, 'usergroup_x', admin_user, 'read', db)
            role_auth.assert_awaited_once_with(mock_request, admin_user.id, 'read', 'usergroup_x', db)

    @pytest.mark.asyncio
    async def test_validate_resource_helper_cover_all_resource_types_and_errors(self, db, org):

        def _db_with_resource(resource):
            session = SimpleNamespace()
            scalars_mock = MagicMock()
            scalars_mock.first.return_value = resource
            result_mock = MagicMock()
            result_mock.scalars.return_value = scalars_mock
            session.execute = AsyncMock(return_value=result_mock)
            return session
        with patch('src.services.users.usergroups.get_resource_config', return_value=SimpleNamespace(resource_type='courses')):
            assert await _validate_resource_exists_and_belongs_to_org('course_x', org.id, _db_with_resource(SimpleNamespace(org_id=org.id)))
        for resource_type, resource_uuid in (('communities', 'community_x'),):
            with patch('src.services.users.usergroups.get_resource_config', return_value=SimpleNamespace(resource_type=resource_type)):
                assert await _validate_resource_exists_and_belongs_to_org(resource_uuid, org.id, _db_with_resource(SimpleNamespace(org_id=org.id)))
        with patch('src.services.users.usergroups.get_resource_config', return_value=SimpleNamespace(resource_type='unsupported')):
            with pytest.raises(HTTPException) as unsupported_exc:
                await _validate_resource_exists_and_belongs_to_org('resource_x', org.id, _db_with_resource(SimpleNamespace(org_id=org.id)))
            assert unsupported_exc.value.status_code == 400
        with patch('src.services.users.usergroups.get_resource_config', return_value=SimpleNamespace(resource_type='communities')):
            with pytest.raises(HTTPException) as missing_exc:
                await _validate_resource_exists_and_belongs_to_org('resource_missing', org.id, _db_with_resource(None))
            assert missing_exc.value.status_code == 404
        with patch('src.services.users.usergroups.get_resource_config', return_value=SimpleNamespace(resource_type='communities')):
            with pytest.raises(HTTPException) as no_org_exc:
                await _validate_resource_exists_and_belongs_to_org('resource_no_org', org.id, _db_with_resource(SimpleNamespace()))
            assert no_org_exc.value.status_code == 500
        with patch('src.services.users.usergroups.get_resource_config', return_value=SimpleNamespace(resource_type='communities')):
            with pytest.raises(HTTPException) as forbidden_exc:
                await _validate_resource_exists_and_belongs_to_org('resource_other_org', org.id, _db_with_resource(SimpleNamespace(org_id=org.id + 1)))
            assert forbidden_exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_usergroup_crud_and_listing(self, mock_request, db, org, admin_user):
        with patch('src.services.users.usergroups.rbac_check', new_callable=AsyncMock), patch('src.services.users.usergroups.check_limits_with_usage'), patch('src.services.users.usergroups.increase_feature_usage'), patch('src.services.users.usergroups.decrease_feature_usage'), patch('src.services.users.usergroups.dispatch_webhooks', new_callable=AsyncMock):
            created = await create_usergroup(mock_request, db, admin_user, UserGroupCreate(name='UG', description='Desc', org_id=org.id))
            fetched = await read_usergroup_by_id(mock_request, db, admin_user, created.id)
            listed = await read_usergroups_by_org_id(mock_request, db, admin_user, org.id)
            updated = await update_usergroup_by_id(mock_request, db, admin_user, created.id, UserGroupUpdate(name='Updated'))
            updated_description = await update_usergroup_by_id(mock_request, db, admin_user, created.id, UserGroupUpdate(description='Updated Desc'))
            deleted = await delete_usergroup_by_id(mock_request, db, admin_user, created.id)
        assert created.name == 'UG'
        assert fetched.usergroup_uuid == created.usergroup_uuid
        assert len(listed) == 1
        assert updated.name == 'Updated'
        assert updated_description.description == 'Updated Desc'
        assert deleted == 'UserGroup deleted successfully'

    @pytest.mark.asyncio
    async def test_usergroup_missing_and_empty_branches(self, mock_request, db, org, admin_user):
        with patch('src.services.users.usergroups.rbac_check', new_callable=AsyncMock), patch('src.services.users.usergroups.check_limits_with_usage'), patch('src.services.users.usergroups.increase_feature_usage'), patch('src.services.users.usergroups.dispatch_webhooks', new_callable=AsyncMock):
            _ = await create_usergroup(mock_request, db, admin_user, UserGroupCreate(name='UG', description='Desc', org_id=org.id))
        with patch('src.services.users.usergroups.rbac_check', new_callable=AsyncMock):
            with pytest.raises(HTTPException) as missing_org_exc:
                await create_usergroup(mock_request, db, admin_user, UserGroupCreate(name='UG2', description='Desc', org_id=999999))
        assert missing_org_exc.value.status_code == 403
        with patch('src.services.users.usergroups.rbac_check', new_callable=AsyncMock):
            with pytest.raises(HTTPException) as missing_group_exc:
                await get_users_linked_to_usergroup(mock_request, db, admin_user, 999999)
            with pytest.raises(HTTPException) as missing_group_resources_exc:
                await get_resources_by_usergroup(mock_request, db, admin_user, 999999)
            with pytest.raises(HTTPException) as missing_group_update_exc:
                await update_usergroup_by_id(mock_request, db, admin_user, 999999, UserGroupUpdate(name='Updated'))
            with pytest.raises(HTTPException) as missing_group_delete_exc:
                await delete_usergroup_by_id(mock_request, db, admin_user, 999999)
            with pytest.raises(HTTPException) as missing_group_add_users_exc:
                await add_users_to_usergroup(mock_request, db, admin_user, 999999, '1')
            with pytest.raises(HTTPException) as missing_group_remove_users_exc:
                await remove_users_from_usergroup(mock_request, db, admin_user, 999999, '1')
            with pytest.raises(HTTPException) as missing_group_add_resources_exc:
                await add_resources_to_usergroup(mock_request, db, admin_user, 999999, 'board_x')
            with pytest.raises(HTTPException) as missing_group_remove_resources_exc:
                await remove_resources_from_usergroup(mock_request, db, admin_user, 999999, 'board_x')
            assert missing_group_exc.value.status_code == 404
            assert missing_group_resources_exc.value.status_code == 404
            assert missing_group_update_exc.value.status_code == 404
            assert missing_group_delete_exc.value.status_code == 404
            assert missing_group_add_users_exc.value.status_code == 404
            assert missing_group_remove_users_exc.value.status_code == 404
            assert missing_group_add_resources_exc.value.status_code == 404
            assert missing_group_remove_resources_exc.value.status_code == 404
        with patch('src.services.users.usergroups.rbac_check', new_callable=AsyncMock), patch('src.services.users.usergroups.check_limits_with_usage'), patch('src.services.users.usergroups.increase_feature_usage'), patch('src.services.users.usergroups.dispatch_webhooks', new_callable=AsyncMock):
            empty_groups = await get_usergroups_by_resource(mock_request, db, admin_user, 'board_not_linked')
        assert empty_groups == []

    @pytest.mark.asyncio
    async def test_add_remove_users_reject_non_integer_ids(self, mock_request, db, org, admin_user):
        with patch('src.services.users.usergroups.rbac_check', new_callable=AsyncMock), patch('src.services.users.usergroups.check_limits_with_usage'), patch('src.services.users.usergroups.increase_feature_usage'), patch('src.services.users.usergroups.dispatch_webhooks', new_callable=AsyncMock):
            usergroup = await create_usergroup(mock_request, db, admin_user, UserGroupCreate(name='UG', description='Desc', org_id=org.id))
        with patch('src.services.users.usergroups.rbac_check', new_callable=AsyncMock):
            with pytest.raises(HTTPException) as add_exc:
                await add_users_to_usergroup(mock_request, db, admin_user, usergroup.id, '1,abc')
            with pytest.raises(HTTPException) as remove_exc:
                await remove_users_from_usergroup(mock_request, db, admin_user, usergroup.id, '1,xyz')
        assert add_exc.value.status_code == 400
        assert 'comma-separated list of integers' in add_exc.value.detail
        assert remove_exc.value.status_code == 400
        assert 'comma-separated list of integers' in remove_exc.value.detail
