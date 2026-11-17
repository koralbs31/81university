from unittest.mock import AsyncMock, patch
import src.router as root_router

async def test_get_non_api_token_user_delegates():
    with patch.object(root_router, "require_non_api_token_user", new=AsyncMock(return_value="delegated-user")) as require:
        result = await root_router.get_non_api_token_user("user")
    require.assert_awaited_once_with("user")
    assert result == "delegated-user"
