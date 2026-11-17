"""
Coverage for the media_/board_/playground_ resource-type resolution branches.

Targets:
- src/security/rbac/utils.py            26, 96, 154, 155
- src/security/rbac/resource_access.py  845-847, 850-852, 855-856
- src/security/rbac/rbac.py             158-163

These tests exercise the real RBAC resolution functions (no RBAC bypass) with
Media, Board and Playground rows inserted directly via their model classes.
"""
from unittest.mock import Mock
from starlette.requests import Request
from src.security.rbac.resource_access import ResourceAccessChecker
from src.db.users import AnonymousUser

class TestGetResourceMediaBoardPlayground:

    def _checker(self, db):
        request = Mock(spec=Request)
        request.state = type('S', (), {})()
        return ResourceAccessChecker(request, db, AnonymousUser())
