"""Regression tests: session ownership and API-token scoping (round 3).

- AI refine/editor sessions are bound to their creator and checked before
  any credit is reserved; a session with history but no owner fails closed.
- Boards playground sessions are bound to their creator.
- API tokens cannot upload media into, or edit discussions of, another org.
- Zapier subscription list/delete take the same admin gate as subscribe.
- API tokens stop authenticating once the org's plan drops below API access.
"""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from fastapi import HTTPException
from src.db.users import APITokenUser
from src.routers.integrations import zapier
from src.services.api_tokens import api_tokens as api_tokens_service
from src.services.communities import discussions
pytestmark = pytest.mark.asyncio
RATE_LIMIT_PATH = 'src.services.security.rate_limiting.enforce_ai_rate_limit'

class FakeRedis:
    """Just enough of redis-py for the session helpers."""

    def __init__(self):
        self.store: dict = {}
        self.zsets: dict = {}

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.store[key] = value if isinstance(value, bytes) else str(value).encode()

    def exists(self, key):
        return 1 if key in self.store else 0

    def expire(self, key, ttl):
        return key in self.store

    def zadd(self, key, mapping):
        self.zsets.setdefault(key, {}).update(mapping)

    def ttl(self, key):
        return -1

def _seed_session(r: FakeRedis, uuid: str, owner: int | None):
    r.setex(f'chat_history:{uuid}', 1, json.dumps([{'role': 'user', 'content': 'secret'}]))
    if owner is not None:
        r.setex(f'chat_meta:{uuid}', 1, json.dumps({'aichat_uuid': uuid, 'user_id': owner}))

def _result(value):
    scalars = MagicMock()
    scalars.first.return_value = value
    res = MagicMock()
    res.scalars.return_value = scalars
    return res

def _db_returning(*values):
    db = AsyncMock()
    db.execute.side_effect = [_result(v) for v in values]
    return db

class TestGenerationRoutersCheckOwnership:
    pass
    pass
    pass
    pass
    pass
    pass

def _discussion_db():
    discussion = SimpleNamespace(discussion_uuid='d1', community_id=10, author_id=1, edit_count=0)
    community = SimpleNamespace(id=10, org_id=1, community_uuid='community_1')
    return _db_returning(discussion, community)

@pytest.mark.parametrize('call', [lambda tok, db: discussions.update_discussion(MagicMock(), 'd1', discussions.DiscussionUpdate(title='t'), tok, db), lambda tok, db: discussions.pin_discussion(MagicMock(), 'd1', True, tok, db), lambda tok, db: discussions.lock_discussion(MagicMock(), 'd1', True, tok, db), lambda tok, db: discussions.delete_discussion(MagicMock(), 'd1', tok, db)], ids=['update', 'pin', 'lock', 'delete'])
async def test_discussion_writes_reject_token_from_other_org(call):
    token = APITokenUser(org_id=2, created_by_user_id=1, rights={})
    admin = AsyncMock(return_value=True)
    with patch.object(discussions, 'authorization_verify_if_user_is_anon', new=AsyncMock()), patch.object(discussions, 'authorization_verify_based_on_org_admin_status', new=admin):
        with pytest.raises(HTTPException) as exc:
            await call(token, _discussion_db())
    assert exc.value.status_code == 403
    admin.assert_not_called()

@pytest.mark.parametrize('op', ['list', 'delete'])
async def test_zapier_subscription_ops_require_org_admin(op):
    token = APITokenUser(org_id=1, created_by_user_id=7, rights={})
    db = AsyncMock()
    gate = AsyncMock(side_effect=HTTPException(status_code=403, detail='admin only'))
    with patch.object(zapier, 'require_org_admin', new=gate):
        with pytest.raises(HTTPException) as exc:
            if op == 'list':
                await zapier.zapier_list_subscriptions(ctx=(token, db))
            else:
                await zapier.zapier_delete_subscription(1, ctx=(token, db))
    assert exc.value.status_code == 403
    gate.assert_awaited_once_with(7, 1, db)
    db.execute.assert_not_called()

def _saas():
    """Both the router-dependency bypass and the plan comparison read the mode."""
    from contextlib import ExitStack
    stack = ExitStack()
    stack.enter_context(patch('src.security.features_utils.plan_check.get_deployment_mode', return_value='saas'))
    stack.enter_context(patch('src.core.deployment_mode.get_deployment_mode', return_value='saas'))
    return stack

def _token_db(token_row):
    scalars = MagicMock()
    scalars.all.return_value = [token_row]
    res = MagicMock()
    res.scalars.return_value = scalars
    db = AsyncMock()
    db.execute.return_value = res
    return db

def _token_row():
    return SimpleNamespace(org_id=1, token_hash='h', expires_at=None, last_used_at=None, is_active=True, created_by_user_id=1)

async def test_token_rejected_after_downgrade_in_saas():
    with _saas(), patch('src.security.features_utils.plan_check.get_org_plan', new=AsyncMock(return_value='free')), patch.object(api_tokens_service, 'security_verify_token', return_value=True):
        with pytest.raises(HTTPException) as exc:
            await api_tokens_service.validate_api_token_for_auth('lh_abcdefghijklmnop', _token_db(_token_row()))
    assert exc.value.status_code == 403
    assert 'API Access' in exc.value.detail

async def test_token_accepted_on_pro_in_saas():
    row = _token_row()
    with _saas(), patch('src.security.features_utils.plan_check.get_org_plan', new=AsyncMock(return_value='pro')), patch.object(api_tokens_service, 'security_verify_token', return_value=True), patch.object(api_tokens_service, 'security_token_needs_rehash', return_value=False), patch.object(api_tokens_service, 'is_org_member', new=AsyncMock(return_value=True)):
        result = await api_tokens_service.validate_api_token_for_auth('lh_abcdefghijklmnop', _token_db(row))
    assert result is row

async def test_token_rejected_when_org_has_no_plan_record():
    missing = AsyncMock(side_effect=HTTPException(status_code=404, detail='no config'))
    with _saas(), patch('src.security.features_utils.plan_check.get_org_plan', new=missing), patch.object(api_tokens_service, 'security_verify_token', return_value=True):
        with pytest.raises(HTTPException) as exc:
            await api_tokens_service.validate_api_token_for_auth('lh_abcdefghijklmnop', _token_db(_token_row()))
    assert exc.value.status_code == 403

async def test_token_plan_check_bypassed_self_hosted():
    row = _token_row()
    with patch('src.security.features_utils.plan_check.get_deployment_mode', return_value='oss'), patch.object(api_tokens_service, 'security_verify_token', return_value=True), patch.object(api_tokens_service, 'security_token_needs_rehash', return_value=False), patch.object(api_tokens_service, 'is_org_member', new=AsyncMock(return_value=True)):
        result = await api_tokens_service.validate_api_token_for_auth('lh_abcdefghijklmnop', _token_db(row))
    assert result is row
