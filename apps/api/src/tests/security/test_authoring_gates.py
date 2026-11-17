"""Second sweep: AI authoring gates, session ownership, listings, CORS default.

Members could spend an org's AI credits on authoring tools (scenario, images,
audio, magic blocks, planning) and read or continue each other's AI sessions;
private boards were listed to every member; comments skipped the rich-content
validator; and a catch-all ``allowed_regexp`` opened CORS with credentials.
"""
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from src.core.middleware.cors import effective_allowed_regexp, get_cors_origin_regex

def _hosting(allowed_regexp, tenancy='multi'):
    return SimpleNamespace(hosting_config=SimpleNamespace(tenancy=tenancy, allowed_regexp=allowed_regexp, frontend_domain='app.example.com', domain='example.com'))

class TestCatchAllAllowedRegexpIsIgnored:

    def test_shipped_default_does_not_open_every_origin(self):
        default = '\\b((?:https?://)[^\\s/$.?#].[^\\s]*)\\b'
        with patch('src.core.middleware.cors.get_learnhouse_config', return_value=_hosting(default)):
            assert effective_allowed_regexp() is None
            regex = get_cors_origin_regex()
        assert re.match(regex, 'https://tenant.example.com')
        assert not re.match(regex, 'https://attacker.invalid')

    def test_scoped_pattern_is_kept(self):
        scoped = '^https://(?:[a-z0-9-]+\\.)*acme\\.test$'
        with patch('src.core.middleware.cors.get_learnhouse_config', return_value=_hosting(scoped)):
            assert effective_allowed_regexp() == scoped

class TestCommentsGoThroughTheRichContentValidator:

    async def test_validate_comment_content_calls_rich_validator(self):
        from src.services.communities import moderation
        fake_db = MagicMock()
        fake_db.execute = AsyncMock(return_value=MagicMock())
        with patch.object(moderation, 'get_community_settings', return_value={}), patch.object(moderation, 'validate_rich_content') as rich, patch.object(moderation, 'validate_content_for_community', new=AsyncMock()):
            await moderation.validate_comment_content('{"type":"doc"}', 1, fake_db)
        rich.assert_called_once()
        assert rich.call_args.kwargs.get('content_type') == 'reply'
