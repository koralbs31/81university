"""Edge branches across smaller services: webhook endpoint cache and log
pruning, playground sessions and rights, explore sorting, library search,
nudges, media streaming, and a few string helpers."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from sqlalchemy.dialects import postgresql
from src.db.courses.courses import Course
from src.db.organizations import Organization
from src.services.communities.moderation import extract_youtube_video_id
from src.services.email.safe_text import header_safe
from src.services.email.utils import get_org_square_logo_url
from src.services.explore.explore import _get_sort_expression, search_orgs_for_explore
from src.services.search.search import _ilike_terms
from src.services.webhooks import dispatch

class _BrokenRedis:

    def get(self, *_a, **_k):
        raise ConnectionError('redis down')

    def set(self, *_a, **_k):
        raise ConnectionError('redis down')

    def delete(self, *_a, **_k):
        raise ConnectionError('redis down')

def test_active_endpoint_cache_reads_the_flag():
    redis = Mock()
    with patch('src.core.redis.get_redis_client', return_value=redis):
        redis.get.return_value = b'1'
        assert dispatch._active_cache_get(5) is True
        redis.get.return_value = '0'
        assert dispatch._active_cache_get(5) is False
        redis.get.return_value = None
        assert dispatch._active_cache_get(5) is None
    redis.get.assert_called_with('webhooks:has_active:5')

def test_active_endpoint_cache_without_redis_is_a_noop():
    with patch('src.core.redis.get_redis_client', return_value=None):
        assert dispatch._active_cache_get(5) is None
        assert dispatch._active_cache_set(5, True) is None
        assert dispatch.invalidate_active_endpoint_cache(5) is None

def test_active_endpoint_cache_tolerates_redis_errors():
    """Redis trouble falls back to querying the database; it never raises."""
    with patch('src.core.redis.get_redis_client', return_value=_BrokenRedis()):
        assert dispatch._active_cache_get(5) is None
        assert dispatch._active_cache_set(5, False) is None
        assert dispatch.invalidate_active_endpoint_cache(5) is None

async def test_prune_delivery_logs_swallows_database_errors():

    def broken_factory():
        raise RuntimeError('db down')
    with patch.object(dispatch, '_async_session_factory', broken_factory), patch.object(dispatch.logger, 'warning') as warn:
        await dispatch._prune_delivery_logs(7)
    warn.assert_called_once()

def _session_returning(*rows):
    results = []
    for row in rows:
        result = Mock()
        result.scalars.return_value.first.return_value = row
        results.append(result)
    return SimpleNamespace(execute=AsyncMock(side_effect=results))

def test_explore_sort_is_salted_only_for_safe_salts():
    assert _get_sort_expression('bad salt!') is Organization.name
    salted = _get_sort_expression('abc-123')
    assert salted is not Organization.name
    assert 'md5' in str(salted.compile(dialect=postgresql.dialect()))

async def test_explore_search_filters_by_label(db, mock_request):
    now = str(datetime.now())
    for i, label in enumerate(['science', 'art'], start=1):
        db.add(Organization(id=10 + i, name=f'Academy {label}', slug=f'academy-{label}', email=f'{label}@x.com', org_uuid=f'org_{label}', explore=True, label=label, creation_date=now, update_date=now))
    await db.commit()
    results = await search_orgs_for_explore(mock_request, db, 'academy', label='science')
    assert [o.slug for o in results] == ['academy-science']

async def test_nudge_run_survives_a_failed_rollback_after_reconcile(db):
    from src.services.nudges.runner import run_nudges
    now = datetime(2026, 11, 4, tzinfo=timezone.utc)
    with patch('src.services.nudges.delivery.reconcile_delivery', new=AsyncMock(side_effect=RuntimeError('provider down'))), patch.object(db, 'rollback', new=AsyncMock(side_effect=RuntimeError('connection lost'))) as rollback:
        stats = await run_nudges(db, seed=True, force=True, now=now)
    rollback.assert_awaited_once()
    assert stats.sent == 0

def test_malformed_youtube_url_is_not_a_video():
    assert extract_youtube_video_id('http://[::1') is None

def test_ilike_terms_without_terms_matches_everything():
    assert str(_ilike_terms([Course.name], [])) == 'true'

def test_header_safe_handles_empty_values():
    assert header_safe('') == ''
    assert header_safe(None) == ''

def test_square_logo_needs_a_media_host():
    org = SimpleNamespace(org_uuid='org_x')
    config = SimpleNamespace(config={'general': {'square_logo_image': 'sq.png'}})
    with patch('src.services.email.utils.get_media_base_url', return_value=''):
        assert get_org_square_logo_url(org, config) is None
    with patch('src.services.email.utils.get_media_base_url', return_value='https://media.test'):
        assert get_org_square_logo_url(org, config) == 'https://media.test/content/orgs/org_x/square_logos/sq.png'
