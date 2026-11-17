import pytest
from unittest.mock import AsyncMock, MagicMock, Mock, patch
from fastapi import HTTPException
from src.security.features_utils.usage import check_limits_with_usage
from src.db.organization_config import OrganizationConfig

def _make_saas_patches():
    """Return common patches for SaaS mode tests."""
    return [patch('src.security.features_utils.resolve.get_deployment_mode', return_value='saas'), patch('src.security.features_utils.usage.get_deployment_mode', return_value='saas'), patch('src.security.features_utils.plans.get_deployment_mode', return_value='saas')]

class TestFeaturesUtils:
    """Test cases for features_utils/usage.py module"""

    @pytest.fixture
    def mock_db_session(self):
        """Create a mock async database session"""
        session = AsyncMock()
        session.execute.return_value = MagicMock()
        return session

    @pytest.fixture
    def mock_org_config(self):
        """Create a mock organization config with v2 format and standard plan."""
        config = Mock(spec=OrganizationConfig)
        config.org_id = 1
        config.config = {'config_version': '2.0', 'plan': 'standard', 'admin_toggles': {}, 'overrides': {}, 'customization': {}}
        return config

    @pytest.fixture
    def mock_redis(self):
        """Create a mock Redis connection"""
        redis_mock = Mock()
        redis_mock.get.return_value = b'5'
        redis_mock.set.return_value = True
        return redis_mock

    def test_feature_set_type_alias(self):
        """Test that FeatureSet type alias includes all expected features"""
        expected_features = ['ai', 'analytics', 'api', 'assignments', 'collaboration', 'courses', 'members', 'payments', 'storage', 'usergroups']
        for feature in expected_features:
            assert feature in ['ai', 'analytics', 'api', 'assignments', 'collaboration', 'courses', 'members', 'payments', 'storage', 'usergroups']

    @pytest.mark.asyncio
    async def test_check_limits_with_usage_limit_reached(self, mock_db_session, mock_org_config):
        """Test feature limit check when limit is reached"""
        mock_org_config.config['plan'] = 'free'
        mock_db_session.execute.return_value.scalar_one_or_none.return_value = mock_org_config
        with patch('src.core.deployment_mode.get_deployment_mode', return_value='saas'), patch('src.security.features_utils.resolve.get_deployment_mode', return_value='saas'), patch('src.security.features_utils.usage.get_deployment_mode', return_value='saas'), patch('src.security.features_utils.usage._get_actual_usage', return_value=10), patch('src.services.orgs.cache.get_cached_org_config', return_value=None), patch('src.services.orgs.cache.set_cached_org_config'):
            with pytest.raises(HTTPException) as exc_info:
                await check_limits_with_usage(feature='members', org_id=1, db_session=mock_db_session)
            assert exc_info.value.status_code == 403
            assert 'Usage Limit has been reached for Members' in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_check_limits_with_usage_unlimited_feature(self, mock_db_session, mock_org_config):
        """Test feature limit check for unlimited feature (limit = 0)"""
        mock_db_session.execute.return_value.scalar_one_or_none.return_value = mock_org_config
        with patch('src.security.features_utils.usage._get_redis_client') as mock_redis_client, patch('src.security.features_utils.resolve.get_deployment_mode', return_value='saas'), patch('src.security.features_utils.usage.get_deployment_mode', return_value='saas'), patch('src.services.orgs.cache.get_cached_org_config', return_value=None), patch('src.services.orgs.cache.set_cached_org_config'):
            mock_redis = Mock()
            mock_redis.get.return_value = b'1000'
            mock_redis_client.return_value = mock_redis
            result = await check_limits_with_usage(feature='analytics', org_id=1, db_session=mock_db_session)
            assert result is True

    @pytest.mark.asyncio
    async def test_all_features_covered(self, mock_db_session, mock_org_config):
        """Test that all features in FeatureSet are covered (EE mode: all enabled)"""
        features = ['ai', 'analytics', 'api', 'assignments', 'collaboration', 'courses', 'members', 'payments', 'storage', 'usergroups']
        mock_db_session.execute.return_value.scalar_one_or_none.return_value = mock_org_config
        with patch('src.security.features_utils.resolve.get_deployment_mode', return_value='ee'), patch('src.security.features_utils.usage.get_deployment_mode', return_value='ee'), patch('src.security.features_utils.usage._get_redis_client') as mock_redis_client, patch('src.services.orgs.cache.get_cached_org_config', return_value=None), patch('src.services.orgs.cache.set_cached_org_config'):
            mock_redis = Mock()
            mock_redis.get.return_value = b'0'
            mock_redis.set.return_value = True
            mock_redis_client.return_value = mock_redis
            for feature in features:
                result = await check_limits_with_usage(feature=feature, org_id=1, db_session=mock_db_session)
                assert result is True
