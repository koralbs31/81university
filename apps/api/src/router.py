from fastapi import APIRouter, Depends
from src.routers import admin as admin_router_module
from src.routers import code_execution
from src.routers import code_submissions
from src.routers import health
from src.routers import demo as demo_router_module
from src.routers import instance
from src.routers import plans
from src.routers import usergroups
from src.routers import editor_templates as editor_templates_router_module
from src.routers import editor_variables as editor_variables_router_module
from src.routers import dev, trail, users, auth, orgs, roles, search
from src.routers import mfa as mfa_router_module
from src.routers import monitoring
from src.routers import nudges as nudges_router_module
from src.routers import stream
from src.routers import api_tokens
from src.routers import webhooks
from src.routers.integrations import zapier as zapier_integration
from src.routers.orgs import custom_domains
from src.routers.courses import chapters, courses, assignments, certifications
from src.routers.paths import paths as paths_router_module
from src.routers.messaging import messaging as messaging_router_module
from src.routers.communities import communities as communities_router_module
from src.routers.communities import discussions as discussions_router_module
from src.routers.courses.activities import activities, blocks
from src.core.ee_hooks import register_ee_routers
from src.services.dev.dev import isDevModeEnabledOrRaise
from src.routers.utils import router as utils_router
from src.security.auth import get_current_user
from src.security.api_token_utils import get_authenticated_non_api_token_user, require_authenticated_user_or_api_token, require_non_api_token_user
from src.security.features_utils.plan_check import require_plan, require_plan_for_certifications, require_plan_for_community, require_plan_for_usergroups, require_plan_for_paths
v1_router = APIRouter(prefix='/api/v1')

async def get_non_api_token_user(user=Depends(get_current_user)):
    """Dependency that rejects API token access."""
    return await require_non_api_token_user(user)
require_authenticated_user = get_authenticated_non_api_token_user
v1_router.include_router(users.router, prefix='/users', tags=['users'], dependencies=[Depends(get_non_api_token_user)])
v1_router.include_router(usergroups.router, prefix='/usergroups', tags=['usergroups'], dependencies=[Depends(require_authenticated_user_or_api_token), Depends(require_plan_for_usergroups('standard', 'User Groups'))])
v1_router.include_router(auth.router, prefix='/auth', tags=['auth'])
v1_router.include_router(mfa_router_module.router, prefix='/auth', tags=['auth'])
v1_router.include_router(orgs.router, prefix='/orgs', tags=['orgs'], dependencies=[Depends(get_non_api_token_user)])
v1_router.include_router(roles.router, prefix='/roles', tags=['roles'], dependencies=[Depends(require_authenticated_user)])
v1_router.include_router(api_tokens.router, prefix='/orgs', tags=['api-tokens'], dependencies=[Depends(require_authenticated_user), Depends(require_plan('pro', 'API Access'))])
v1_router.include_router(webhooks.router, prefix='/orgs', tags=['webhooks'], dependencies=[Depends(require_authenticated_user), Depends(require_plan('pro', 'Webhooks'))])
v1_router.include_router(zapier_integration.router, prefix='/integrations/zapier', tags=['integrations', 'zapier'])
v1_router.include_router(custom_domains.router, prefix='/orgs', tags=['custom-domains'], dependencies=[Depends(require_authenticated_user), Depends(require_plan('standard', 'Custom Domains'))])
v1_router.include_router(custom_domains.public_router, prefix='/orgs', tags=['custom-domains'])
v1_router.include_router(nudges_router_module.public_router, prefix='/emails', tags=['emails'])
v1_router.include_router(nudges_router_module.internal_router, prefix='/internal/emails', tags=['emails-internal'])
v1_router.include_router(custom_domains.internal_router, prefix='/internal', tags=['custom-domains-internal'])

v1_router.include_router(blocks.router, prefix='/blocks', tags=['blocks'], dependencies=[Depends(get_non_api_token_user)])
v1_router.include_router(admin_router_module.router, prefix='/admin', tags=['admin'])
v1_router.include_router(courses.router, prefix='/courses', tags=['courses'])
v1_router.include_router(search.router, prefix='/search', tags=['search'])
v1_router.include_router(assignments.router, prefix='/assignments', tags=['assignments'], dependencies=[Depends(require_authenticated_user_or_api_token)])
v1_router.include_router(chapters.router, prefix='/chapters', tags=['chapters'])
v1_router.include_router(activities.router, prefix='/activities', tags=['activities'])
v1_router.include_router(editor_templates_router_module.router, prefix='/editor-templates', tags=['editor-templates'], dependencies=[Depends(require_authenticated_user)])
v1_router.include_router(editor_variables_router_module.router, prefix='/editor-variables', tags=['editor-variables'], dependencies=[Depends(require_authenticated_user)])
v1_router.include_router(paths_router_module.certificates_router, prefix='/paths', tags=['paths'])
v1_router.include_router(paths_router_module.router, prefix='/paths', tags=['paths'], dependencies=[Depends(require_plan_for_paths('pro', 'Paths'))])
v1_router.include_router(messaging_router_module.router, prefix='/messaging', tags=['messaging'])
v1_router.include_router(communities_router_module.router, prefix='/communities', tags=['communities'], dependencies=[Depends(require_plan_for_community('standard', 'Communities'))])
v1_router.include_router(discussions_router_module.router, tags=['discussions'], dependencies=[Depends(require_plan_for_community('standard', 'Communities'))])
v1_router.include_router(certifications.router, prefix='/certifications', tags=['certifications'], dependencies=[Depends(require_plan_for_certifications('pro', 'Certifications'))])
v1_router.include_router(trail.router, prefix='/trail', tags=['trail'], dependencies=[Depends(require_authenticated_user)])
v1_router.include_router(code_execution.router, prefix='/code', tags=['code-execution'], dependencies=[Depends(require_authenticated_user)])
v1_router.include_router(code_submissions.router, prefix='/code/submissions', tags=['code_submissions'], dependencies=[Depends(require_authenticated_user)])
v1_router.include_router(instance.router, prefix='/instance', tags=['instance'])
v1_router.include_router(demo_router_module.router, prefix='/demo', tags=['demo'])
v1_router.include_router(monitoring.router, prefix='/monitoring', tags=['monitoring'], dependencies=[Depends(get_non_api_token_user)])
v1_router.include_router(plans.router, prefix='/plans', tags=['plans'])
register_ee_routers(v1_router)
v1_router.include_router(health.router, prefix='/health', tags=['health'], dependencies=[Depends(get_non_api_token_user)])
v1_router.include_router(dev.router, prefix='/dev', tags=['dev'], dependencies=[Depends(isDevModeEnabledOrRaise), Depends(get_non_api_token_user)])
v1_router.include_router(utils_router, prefix='/utils', tags=['utils'], dependencies=[Depends(require_authenticated_user)])
v1_router.include_router(stream.router, prefix='/stream', tags=['stream'], dependencies=[Depends(get_non_api_token_user)])
