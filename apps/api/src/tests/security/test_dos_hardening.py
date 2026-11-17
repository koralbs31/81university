"""Availability / cost-abuse hardening: debounced reindex, learner upload
limits, library search bounds, list clamps, AI input caps, caption dedupe,
per-recipient magic links, webhook caps and the link-preview throttle."""
import asyncio
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from fastapi import HTTPException, UploadFile
from starlette.datastructures import Headers
from src.db.webhooks import WebhookEndpoint, WebhookEndpointCreate

class _FakeRedis:
    """SET NX / GET / DELETE / RPUSH stand-in."""

    def __init__(self):
        self.store = {}
        self.lists = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def get(self, key):
        return self.store.get(key)

    def delete(self, key):
        self.store.pop(key, None)

    def rpush(self, key, value):
        self.lists.setdefault(key, []).append(value)

def _deny(*_a, **_k):
    return (False, 999, 42)

class TestReindexDebounce:
    """Saves are debounced by the RAG queue (one run per burst) and orgs
    without AI search are never indexed; both covered in depth in
    test_rag_queue.py and test_rag_pipeline.py."""

    @pytest.fixture
    def rag_dispatch(self):
        """Use the real queue dispatch here, not the suite-wide stub."""
        yield None
    pass
    pass

def _upload(name, data=b'%PDF-1.4 test', size=None):
    return UploadFile(file=BytesIO(data), filename=name, size=size, headers=Headers({}))

class TestSubmissionUpload:

    @pytest.mark.parametrize('name', ['clip.mp4', 'movie.webm', 'pkg.sqlite', 'a.mp3'])
    async def test_video_and_other_types_rejected(self, name):
        from src.services.courses.activities.uploads.sub_file import upload_submission_file
        with pytest.raises(HTTPException) as exc:
            await upload_submission_file(_upload(name), 'a', 'o', 'c', 'as', 't', 7)
        assert exc.value.status_code == 415

    async def test_oversized_document_rejected(self):
        from src.services.courses.activities.uploads import sub_file
        big = _upload('essay.pdf', size=sub_file.SUBMISSION_MAX_SIZE + 1)
        with pytest.raises(HTTPException) as exc:
            await sub_file.upload_submission_file(big, 'a', 'o', 'c', 'as', 't', 7)
        assert exc.value.status_code == 413

    async def test_small_pdf_accepted(self):
        from src.services.courses.activities.uploads import sub_file
        with patch('src.services.utils.upload_content.upload_content', new=AsyncMock()):
            name = await sub_file.upload_submission_file(_upload('essay.pdf'), 'a', 'o', 'c', 'as', 't', 7)
        assert name.endswith('_submission_u7.pdf')

    async def test_router_rate_limits_uploads(self, mock_request, admin_user):
        from src.routers.courses import assignments as router_mod
        service = AsyncMock()
        with patch.object(router_mod, 'check_rate_limit', side_effect=_deny), patch.object(router_mod, 'put_assignment_task_submission_file', new=service):
            with pytest.raises(HTTPException) as exc:
                await router_mod.api_put_assignment_task_sub_file(mock_request, 'task', _upload('essay.pdf'), admin_user, None)
        assert exc.value.status_code == 429
        service.assert_not_awaited()

def _spy_statements(db):
    seen = []
    real = db.execute

    async def _execute(stmt, *a, **k):
        seen.append(stmt)
        return await real(stmt, *a, **k)
    return (seen, _execute)

def _limit_offset(stmt):
    return (stmt._limit_clause.value, stmt._offset_clause.value if stmt._offset_clause is not None else 0)

class TestAISchemaLimits:
    pass
    pass
    pass
    pass
    pass

class TestMagicLinkPerEmail:

    async def test_per_email_cap_returns_generic_without_sending(self, mock_request):
        from src.routers import auth as auth_mod
        body = auth_mod.MagicLinkLoginRequest(email='victim@example.com')
        resolve = AsyncMock()
        with patch.object(auth_mod, 'check_login_rate_limit', return_value=(True, 0)), patch.object(auth_mod, 'check_rate_limit', side_effect=_deny) as rl, patch('src.services.auth.magic_login.resolve_org', new=resolve), patch('src.services.auth.magic_login.send_magic_login_email') as send:
            out = await auth_mod.magic_link_request(mock_request, body, MagicMock())
        assert out == {'detail': 'If an account exists for that email, a login link has been sent.'}
        assert rl.call_args.kwargs['key'] == 'magic_link:victim@example.com'
        resolve.assert_not_awaited()
        send.assert_not_called()

class TestWebhookLimits:

    async def test_endpoint_cap_per_org(self, db, org, admin_user, mock_request, monkeypatch):
        from src.services.webhooks import webhooks
        monkeypatch.setattr(webhooks, 'MAX_WEBHOOK_ENDPOINTS_PER_ORG', 2)
        for i in range(2):
            db.add(WebhookEndpoint(webhook_uuid=f'webhook_{i}', org_id=org.id, url='https://example.com/h', secret_encrypted='x', events=['course_created'], is_active=True, created_by_user_id=admin_user.id, creation_date='', update_date=''))
        await db.commit()
        obj = WebhookEndpointCreate(url='https://example.com/new', events=['course_created'])
        with patch.object(webhooks, 'authorization_verify_if_user_is_anon', new=AsyncMock()), patch.object(webhooks, 'require_org_admin', new=AsyncMock()), patch('src.services.demo.guards.require_not_demo_org', new=AsyncMock()), patch.object(webhooks, '_validate_webhook_url'):
            with pytest.raises(HTTPException) as exc:
                await webhooks.create_webhook_endpoint(mock_request, db, org.id, obj, admin_user)
        assert exc.value.status_code == 400
        assert 'at most 2' in exc.value.detail

    async def test_dispatch_skips_orgs_without_endpoints(self, monkeypatch):
        from src.services.webhooks import dispatch
        monkeypatch.setattr(dispatch, '_active_cache_get', lambda org_id: False)
        monkeypatch.setattr(dispatch, 'validate_event_data', lambda *a: None)
        deliver = AsyncMock()
        monkeypatch.setattr(dispatch, '_deliver_webhooks', deliver)
        await dispatch.dispatch_webhooks('course_created', 1, {})
        await asyncio.sleep(0)
        deliver.assert_not_called()
        await dispatch.dispatch_webhooks('ping', 1, {}, webhook_ids=[3])
        await asyncio.gather(*list(dispatch._background_tasks))
        deliver.assert_awaited_once()

    def test_delivery_concurrency_bounded(self):
        from src.services.webhooks import dispatch
        assert dispatch._delivery_semaphore._value == dispatch.MAX_CONCURRENT_DELIVERIES

class TestRouteThrottles:

    async def test_link_preview_rate_limited_per_user(self, admin_user):
        from src.routers import utils as utils_mod
        fetch = AsyncMock()
        with patch.object(utils_mod, 'check_rate_limit', side_effect=_deny) as rl, patch.object(utils_mod, 'fetch_link_preview', new=fetch):
            with pytest.raises(HTTPException) as exc:
                await utils_mod.link_preview('https://example.com', admin_user)
        assert exc.value.status_code == 429
        assert rl.call_args.kwargs['key'] == f'link_preview:{admin_user.id}'
        fetch.assert_not_awaited()
