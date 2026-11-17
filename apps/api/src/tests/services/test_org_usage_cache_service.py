"""Coverage for src/services/orgs/{usage,cache,uploads}.py."""
from datetime import datetime
from unittest.mock import AsyncMock, Mock, patch
import pytest
from fastapi import HTTPException
from src.db.organization_config import OrganizationConfig
from src.services.orgs import cache as org_cache
from src.services.orgs.cache import _INSTANCE_INFO_KEY
from src.services.orgs import uploads as org_uploads
from src.services.orgs import usage as org_usage

async def _make_org_config(db, org_id: int, config: dict) -> OrganizationConfig:
    row = OrganizationConfig(org_id=org_id, config=config, creation_date=str(datetime.now()), update_date=str(datetime.now()))
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row

class TestOrgCacheHelpers:

    def test_cache_client_and_round_trip_paths(self):
        redis_client = Mock()
        redis_client.get.side_effect = [b'{"slug":"cached-org"}', b'{"instance":"cached"}']
        with patch('src.services.orgs.cache.get_redis_client', return_value=redis_client):
            assert org_cache.get_cached_org_by_slug('cached-org') == {'slug': 'cached-org'}
            org_cache.set_cached_org_by_slug('cached-org', {'slug': 'cached-org'})
            org_cache.invalidate_org_cache('cached-org')
            assert org_cache.get_cached_instance_info() == {'instance': 'cached'}
            org_cache.set_cached_instance_info({'instance': 'cached'})
        redis_client.get.assert_any_call('org_cache:slug:cached-org')
        redis_client.get.assert_any_call(_INSTANCE_INFO_KEY)
        redis_client.setex.assert_any_call('org_cache:slug:cached-org', org_cache.CACHE_TTL_ORG_SLUG, '{"slug": "cached-org"}')
        redis_client.setex.assert_any_call(_INSTANCE_INFO_KEY, org_cache.CACHE_TTL_INSTANCE_INFO, '{"instance": "cached"}')
        redis_client.delete.assert_called_once_with('org_cache:slug:cached-org')

    def test_cache_helpers_handle_missing_client_and_redis_errors(self):
        with patch('src.services.orgs.cache.get_redis_client', return_value=None):
            assert org_cache.get_cached_org_by_slug('missing') is None
            org_cache.set_cached_org_by_slug('missing', {'slug': 'missing'})
            org_cache.invalidate_org_cache('missing')
            assert org_cache.get_cached_instance_info() is None
            org_cache.set_cached_instance_info({'instance': 'missing'})
        redis_client = Mock()
        redis_client.get.side_effect = RuntimeError('boom')
        redis_client.setex.side_effect = RuntimeError('boom')
        redis_client.delete.side_effect = RuntimeError('boom')
        with patch('src.services.orgs.cache.get_redis_client', return_value=redis_client):
            assert org_cache.get_cached_org_by_slug('broken') is None
            org_cache.set_cached_org_by_slug('broken', {'slug': 'broken'})
            org_cache.invalidate_org_cache('broken')
            assert org_cache.get_cached_instance_info() is None
            org_cache.set_cached_instance_info({'instance': 'broken'})
        assert redis_client.get.call_count == 2
        assert redis_client.setex.call_count == 2
        assert redis_client.delete.call_count == 1

    def test_get_redis_client_returns_none_when_core_returns_none(self):
        """get_redis_client from src.core.redis returns None → cache functions
        all return None / no-op silently."""
        with patch('src.services.orgs.cache.get_redis_client', return_value=None):
            assert org_cache.get_cached_org_by_slug('any-slug') is None
            assert org_cache.get_cached_instance_info() is None
            org_cache.set_cached_org_by_slug('any-slug', {})
            org_cache.invalidate_org_cache('any-slug')
            org_cache.set_cached_instance_info({})

class TestOrgUploadWrappers:

    @pytest.mark.asyncio
    @pytest.mark.parametrize('wrapper, file_name, expected_kwargs', [(org_uploads.upload_org_logo, 'logo.png', {'directory': 'logos', 'type_of_dir': 'orgs', 'allowed_types': ['image'], 'filename_prefix': 'logo'}), (org_uploads.upload_org_thumbnail, 'thumb.png', {'directory': 'thumbnails', 'type_of_dir': 'orgs', 'allowed_types': ['image'], 'filename_prefix': 'thumbnail'}), (org_uploads.upload_org_preview, 'preview.png', {'directory': 'previews', 'type_of_dir': 'orgs', 'allowed_types': ['image'], 'filename_prefix': 'preview'}), (org_uploads.upload_org_landing_content, 'landing.html', {'directory': 'landing', 'type_of_dir': 'orgs', 'allowed_types': ['image', 'video', 'document'], 'filename_prefix': 'landing'}), (org_uploads.upload_org_auth_background, 'background.png', {'directory': 'auth_backgrounds', 'type_of_dir': 'orgs', 'allowed_types': ['image'], 'filename_prefix': 'auth_bg'}), (org_uploads.upload_org_og_image, 'og.png', {'directory': 'og_images', 'type_of_dir': 'orgs', 'allowed_types': ['image'], 'filename_prefix': 'og'}), (org_uploads.upload_org_favicon, 'favicon.ico', {'directory': 'favicons', 'type_of_dir': 'orgs', 'allowed_types': ['image'], 'filename_prefix': 'favicon'}), (org_uploads.upload_org_square_logo, 'square.png', {'directory': 'square_logos', 'type_of_dir': 'orgs', 'allowed_types': ['image'], 'filename_prefix': 'square_logo'})])
    async def test_upload_wrappers_delegate_to_upload_file(self, wrapper, file_name, expected_kwargs):
        upload_file = AsyncMock(return_value='saved-name')
        fake_file = Mock()
        fake_file.filename = file_name
        with patch('src.services.orgs.uploads.upload_file', upload_file):
            result = await wrapper(fake_file, 'org-uuid')
        assert result == 'saved-name'
        upload_file.assert_awaited_once()
        assert upload_file.await_args.kwargs['file'] is fake_file
        assert upload_file.await_args.kwargs['uuid'] == 'org-uuid'
        for key, value in expected_kwargs.items():
            assert upload_file.await_args.kwargs[key] == value

@pytest.mark.asyncio
async def test_resource_usage_reports_counts_and_unlimited_capacity(db, org, mock_request, admin_user):
    await _make_org_config(db, org.id, {})
    with patch.object(org_usage, '_get_actual_usage', AsyncMock(side_effect=[3, 5, 1])), patch.object(org_usage, 'resolve_feature', return_value={'limit': -1}):
        result = await org_usage.get_org_usage_and_limits(mock_request, org.id, admin_user, db)
    assert set(result['features']) == {'courses', 'members', 'admin_seats'}
    assert result['features']['courses'] == {'usage': 3, 'limit': 'unlimited', 'remaining': 'unlimited', 'limit_reached': False}

@pytest.mark.asyncio
async def test_resource_usage_requires_config(db, org, mock_request, admin_user):
    with pytest.raises(HTTPException) as exc:
        await org_usage.get_org_usage_and_limits(mock_request, org.id, admin_user, db)
    assert exc.value.status_code == 404
