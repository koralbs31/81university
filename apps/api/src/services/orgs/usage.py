"""Non-financial resource counts and configured capacity limits."""
from fastapi import HTTPException, Request
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
from src.db.organization_config import OrganizationConfig
from src.security.features_utils.resolve import resolve_feature, _get_plan_from_config
from src.security.features_utils.usage import _get_actual_usage
from src.core.deployment_mode import get_deployment_mode


async def get_org_usage_and_limits(request: Request, org_id: int, current_user, db_session: AsyncSession) -> dict:
    config_row = (await db_session.execute(
        select(OrganizationConfig).where(OrganizationConfig.org_id == org_id)
    )).scalars().first()
    if config_row is None:
        raise HTTPException(status_code=404, detail='Organization config not found')
    config = config_row.config or {}
    features = {}
    for feature in ('courses', 'members', 'admin_seats'):
        usage = await _get_actual_usage(feature, org_id, db_session)
        limit = resolve_feature(feature, config, org_id)['limit']
        features[feature] = {
            'usage': usage,
            'limit': limit if limit > 0 else 'unlimited',
            'remaining': max(0, limit - usage) if limit > 0 else 'unlimited',
            'limit_reached': limit > 0 and usage >= limit,
        }
    return {'org_id': org_id, 'plan': _get_plan_from_config(config), 'mode': get_deployment_mode(), 'features': features}
