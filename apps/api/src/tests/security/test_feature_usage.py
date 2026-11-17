from types import SimpleNamespace
from unittest.mock import Mock, patch, AsyncMock
import pytest
from fastapi import HTTPException
from sqlalchemy import delete
from src.db.organization_config import OrganizationConfig
from src.security.features_utils import usage

async def _make_org_config(db, org_id: int, config: dict) -> OrganizationConfig:
    org_config = OrganizationConfig(org_id=org_id, config=config)
    db.add(org_config)
    await db.commit()
    await db.refresh(org_config)
    return org_config

class TestFeatureUsage:

    def test_get_redis_client_success_and_missing_config(self):
        redis_client = Mock()
        with patch('src.security.features_utils.usage._get_redis_pool_client', return_value=redis_client):
            result = usage._get_redis_client()
        assert result is redis_client
        with patch('src.security.features_utils.usage._get_redis_pool_client', return_value=None):
            with pytest.raises(HTTPException) as exc_info:
                usage._get_redis_client()
        assert exc_info.value.status_code == 500

    def test_get_org_plan_versions(self):
        assert usage._get_org_plan(SimpleNamespace(config={'config_version': '2.0', 'plan': 'pro'})) == 'pro'
        assert usage._get_org_plan(SimpleNamespace(config={'config_version': '1.4', 'cloud': {'plan': 'standard'}})) == 'standard'

    @pytest.mark.asyncio
    async def test_actual_counts_and_dashboard_seats(self, db, org, regular_user, course, admin_user, admin_role):
        assert await usage._get_actual_member_count(org.id, db) == 2
        assert await usage._get_actual_course_count(org.id, db) == 1
        assert await usage._get_actual_admin_seat_count(org.id, db) == 1
        assert usage.is_role_dashboard_enabled(admin_role) is True
        assert usage.is_role_dashboard_enabled(SimpleNamespace(rights=[])) is False

    @pytest.mark.asyncio
    async def test_actual_usage_routes_and_zero_admin_seats(self, db, org, regular_user, course):
        assert await usage._get_actual_admin_seat_count(org.id, db) == 0
        assert await usage._get_actual_usage('members', org.id, db) == 1
        assert await usage._get_actual_usage('courses', org.id, db) == 1
        assert await usage._get_actual_usage('admin_seats', org.id, db) == 0
        assert await usage._get_actual_usage('other', org.id, db) == 0

    def test_invalidate_usage_cache_success_and_swallowed_error(self):
        redis_client = Mock()
        with patch('src.security.features_utils.usage._get_redis_client', return_value=redis_client):
            usage._invalidate_usage_cache(9)
        redis_client.delete.assert_called_once_with('org_usage:9')
        with patch('src.security.features_utils.usage._get_redis_client', side_effect=RuntimeError('boom')):
            usage._invalidate_usage_cache(9)

    @pytest.mark.asyncio
    async def test_feature_enabled_and_access_paths(self, db, org):
        await _make_org_config(db, org.id, {'config_version': '2.0', 'plan': 'standard'})
        with patch('src.security.features_utils.resolve.resolve_feature', return_value={'enabled': True, 'limit': 1}), patch('src.services.orgs.cache.get_cached_org_config', return_value=None), patch('src.services.orgs.cache.set_cached_org_config'):
            assert await usage.check_feature_enabled('courses', org.id, db) is True
        await db.execute(delete(OrganizationConfig).where(OrganizationConfig.org_id == org.id))
        await db.commit()
        with patch('src.services.orgs.cache.get_cached_org_config', return_value=None), patch('src.services.orgs.cache.set_cached_org_config'):
            with pytest.raises(HTTPException) as missing_exc:
                await usage.check_feature_enabled('courses', org.id, db)
        assert missing_exc.value.status_code == 404
        await _make_org_config(db, org.id, {'config_version': '2.0', 'plan': 'standard'})
        with patch('src.security.features_utils.resolve.resolve_feature', return_value={'enabled': False, 'limit': 1}), patch('src.services.orgs.cache.get_cached_org_config', return_value=None), patch('src.services.orgs.cache.set_cached_org_config'):
            with pytest.raises(HTTPException) as disabled_exc:
                await usage.check_feature_enabled('courses', org.id, db)
        assert disabled_exc.value.status_code == 403
        with patch('src.security.features_utils.usage.get_deployment_mode', return_value='oss'):
            assert await usage.check_feature_access('versioning', org.id, db) is True
        with patch('src.security.features_utils.usage.get_deployment_mode', return_value='saas'), patch('src.security.features_utils.usage.get_required_plan_for_feature', return_value=None):
            assert await usage.check_feature_access('custom', org.id, db) is True
        await db.execute(delete(OrganizationConfig).where(OrganizationConfig.org_id == org.id))
        await db.commit()
        with patch('src.security.features_utils.usage.get_deployment_mode', return_value='saas'), patch('src.security.features_utils.usage.get_required_plan_for_feature', return_value='pro'), patch('src.services.orgs.cache.get_cached_org_config', return_value=None), patch('src.services.orgs.cache.set_cached_org_config'):
            with pytest.raises(HTTPException) as missing_plan_exc:
                await usage.check_feature_access('custom', org.id, db)
        assert missing_plan_exc.value.status_code == 404
        await _make_org_config(db, org.id, {'config_version': '2.0', 'plan': 'standard'})
        with patch('src.security.features_utils.usage.get_deployment_mode', return_value='saas'), patch('src.security.features_utils.usage.get_required_plan_for_feature', return_value='pro'), patch('src.security.features_utils.usage.plan_meets_requirement', return_value=False), patch('src.services.orgs.cache.get_cached_org_config', return_value=None), patch('src.services.orgs.cache.set_cached_org_config'):
            with pytest.raises(HTTPException) as denied_exc:
                await usage.check_feature_access('custom', org.id, db)
        assert denied_exc.value.status_code == 403
        with patch('src.security.features_utils.usage.get_deployment_mode', return_value='saas'), patch('src.security.features_utils.usage.get_required_plan_for_feature', return_value='pro'), patch('src.security.features_utils.usage.plan_meets_requirement', return_value=True), patch('src.services.orgs.cache.get_cached_org_config', return_value=None), patch('src.services.orgs.cache.set_cached_org_config'):
            assert await usage.check_feature_access('custom', org.id, db) is True

    @pytest.mark.asyncio
    async def test_admin_seat_helpers_and_purchased_seats(self, db, org, admin_user):
        await _make_org_config(db, org.id, {'config_version': '2.0', 'plan': 'standard'})
        with patch('src.security.features_utils.usage._is_non_saas', return_value=False):
            assert await usage.check_admin_seat_limit(org.id, db) is True
            with patch('src.security.features_utils.usage._get_org_plan', return_value='free'), patch('src.security.features_utils.usage.get_plan_limit', return_value=1), patch('src.security.features_utils.usage._get_actual_admin_seat_count', return_value=1):
                with pytest.raises(HTTPException) as seat_exc:
                    await usage.check_admin_seat_limit(org.id, db)
            assert seat_exc.value.status_code == 403
            assert 'not enabled' not in str(seat_exc.value.detail).lower()
            with patch('src.security.features_utils.usage._get_org_plan', return_value='free'), patch('src.security.features_utils.usage.get_plan_limit', return_value=2), patch('src.security.features_utils.usage._get_actual_admin_seat_count', return_value=1):
                assert await usage.check_admin_seat_limit(org.id, db) is True
            with patch('src.security.features_utils.usage._get_org_plan', return_value='free'), patch('src.security.features_utils.usage.get_plan_limit', return_value=0):
                assert await usage.check_admin_seat_limit(org.id, db) is True
            with patch('src.security.features_utils.usage._get_org_config', new_callable=AsyncMock, return_value=None):
                assert await usage.check_admin_seat_limit(org.id, db) is True
        with patch('src.security.features_utils.usage._is_non_saas', return_value=True):
            assert await usage.check_admin_seat_limit(org.id, db) is True
        with patch('src.security.features_utils.usage.get_plan_limit', return_value=5):
            summary = await usage.get_admin_seat_usage(org.id, db)
        assert summary == {'plan': 'standard', 'current_usage': 1, 'limit': 5, 'remaining': 4}
        await db.execute(delete(OrganizationConfig).where(OrganizationConfig.org_id == org.id))
        await db.commit()
        assert await usage.get_admin_seat_usage(org.id, db) == {'error': 'Organization has no config'}

class TestGetOrgConfigCacheHit:
    """Cover _get_org_config cache-hit and set_cached exception paths in usage.py."""

    @pytest.mark.asyncio
    async def test_returns_cached_org_config_without_db_hit(self, db, org):
        from src.db.organization_config import OrganizationConfig
        cached_data = {'org_id': org.id, 'config': {'plan': 'pro'}}
        with patch('src.services.orgs.cache.get_cached_org_config', return_value=cached_data):
            result = await usage._get_org_config(org.id, db)
        assert isinstance(result, OrganizationConfig)

    @pytest.mark.asyncio
    async def test_invalid_cached_data_falls_back_to_db(self, db, org):
        from src.db.organization_config import OrganizationConfig
        org_config = OrganizationConfig(org_id=org.id, config={'plan': 'free'})
        db.add(org_config)
        await db.commit()
        with patch('src.services.orgs.cache.get_cached_org_config', return_value={'bad': 'data'}), patch('src.services.orgs.cache.set_cached_org_config'):
            result = await usage._get_org_config(org.id, db)
        assert result is not None

    @pytest.mark.asyncio
    async def test_set_cached_exception_is_swallowed(self, db, org):
        from src.db.organization_config import OrganizationConfig
        org_config = OrganizationConfig(org_id=org.id, config={'plan': 'free'})
        db.add(org_config)
        await db.commit()
        with patch('src.services.orgs.cache.get_cached_org_config', return_value=None), patch('src.services.orgs.cache.set_cached_org_config', side_effect=RuntimeError('redis')):
            result = await usage._get_org_config(org.id, db)
        assert result is not None

    @pytest.mark.asyncio
    async def test_invalid_cached_data_exception_falls_back_to_db(self, db, org):
        from src.db.organization_config import OrganizationConfig
        org_config = OrganizationConfig(org_id=org.id, config={'plan': 'free'})
        db.add(org_config)
        await db.commit()
        with patch('src.services.orgs.cache.get_cached_org_config', return_value={1: 'bad-key'}), patch('src.services.orgs.cache.set_cached_org_config'):
            result = await usage._get_org_config(org.id, db)
        assert result is not None
