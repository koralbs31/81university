"""Editor-AI content cap, org-scoped member view, Zapier webhook cache
invalidation, and the demo org's per-visitor AI cap."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

def test_organization_user_is_typed_as_org_member_view():
    from src.db.organizations import OrganizationUser
    from src.db.roles import RoleRead
    from src.db.users import OrgMemberUserRead
    assert OrganizationUser.model_fields['user'].annotation is OrgMemberUserRead
    role = RoleRead.model_construct(id=1, name='r')
    member = OrgMemberUserRead(id=1, user_uuid='u', username='n', first_name='f', last_name='l', email='e@x.io')
    ou = OrganizationUser.model_construct(user=member, role=role)
    dumped = OrganizationUser.model_validate({'user': {**member.model_dump(), 'is_superadmin': True}, 'role': role}).model_dump()
    assert 'is_superadmin' not in dumped['user']
    assert ou.user.email == 'e@x.io'

def _zapier_session(endpoint=None):
    session = MagicMock()
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    session.delete = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.first.return_value = endpoint
    session.execute = AsyncMock(return_value=result)
    return session

def test_zapier_subscribe_invalidates_active_cache():
    from src.routers.integrations import zapier
    api_user = SimpleNamespace(org_id=7, created_by_user_id=3, rights={'users': {'action_read': True}, 'courses': {'action_read': True}})
    session = _zapier_session()
    payload = zapier.ZapierSubscriptionCreate(target_url='https://hooks.zapier.com/x', event='course_created', zap_id='z', zap_name='n')
    with patch.object(zapier, 'require_org_admin', new=AsyncMock()), patch.object(zapier, '_validate_webhook_url'), patch.object(zapier, '_validate_event'), patch.object(zapier, 'encrypt_secret', return_value='enc'), patch.object(zapier, '_enforce_webhook_rate_limit'), patch.object(zapier, 'enforce_webhook_endpoint_cap', new=AsyncMock()), patch.object(zapier, 'invalidate_active_endpoint_cache') as inv:
        asyncio.run(zapier.zapier_create_subscription(MagicMock(), payload, ctx=(api_user, session)))
    inv.assert_called_once_with(7)

def test_zapier_unsubscribe_invalidates_active_cache():
    from src.routers.integrations import zapier
    api_user = SimpleNamespace(org_id=7, created_by_user_id=3, rights={'users': {'action_read': True}})
    session = _zapier_session(endpoint=MagicMock())
    with patch.object(zapier, 'require_org_admin', new=AsyncMock()), patch.object(zapier, 'invalidate_active_endpoint_cache') as inv:
        asyncio.run(zapier.zapier_delete_subscription(5, ctx=(api_user, session)))
    inv.assert_called_once_with(7)

class _Redis:

    def __init__(self):
        self.store: dict[str, int] = {}

    def incr(self, key):
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    def expire(self, key, ttl):
        return True
