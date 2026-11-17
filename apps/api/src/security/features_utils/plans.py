"""
Plan-based feature restriction utilities.

Single source of truth for plan hierarchy, feature configs, and limits.
Org config only stores the plan name (cloud.plan); all feature settings
are derived from these definitions at runtime.
"""
from typing import Literal
PlanLevel = Literal['free', 'personal', 'personal-family', 'standard', 'pro', 'enterprise']
PLAN_HIERARCHY: list[str] = ['free', 'personal', 'personal-family', 'standard', 'pro', 'enterprise']
FEATURE_PLAN_REQUIREMENTS: dict[str, PlanLevel] = {'analytics': 'standard', 'communities': 'standard', 'drip': 'standard', 'live_sessions': 'pro', 'seo': 'standard', 'usergroups': 'standard', 'api_tokens': 'pro', 'webhooks': 'pro', 'certifications': 'pro', 'paths': 'pro', 'messaging': 'free', 'custom_domains': 'pro', 'roles': 'pro', 'versioning': 'pro', 'analytics_advanced': 'pro', 'audit_logs': 'enterprise', 'scorm': 'enterprise', 'sso': 'enterprise', 'lti': 'enterprise'}
PLAN_FEATURE_CONFIGS: dict[str, dict] = {'free': {'features': {'analytics': {'enabled': False, 'limit': 0}, 'api': {'enabled': False, 'limit': 0}, 'assignments': {'enabled': True, 'limit': 5}, 'courses': {'enabled': True, 'limit': 1}, 'members': {'admin_limit': 1, 'enabled': True, 'limit': 10}, 'usergroups': {'enabled': False, 'limit': 0}, 'live_sessions': {'enabled': False, 'limit': 0}, 'paths': {'enabled': False}, 'messaging': {'enabled': True}, 'communities': {'enabled': False}, 'roles': {'enabled': False}, 'scorm': {'enabled': False}, 'sso': {'enabled': False}, 'lti': {'enabled': False}, 'versioning': {'enabled': False}, 'webhooks': {'enabled': False, 'limit': 0}, 'audit_logs': {'enabled': False}}, 'general': {'watermark': True}, 'cloud': {'plan': 'free', 'custom_domain': False}}, 'personal': {'features': {'analytics': {'enabled': False, 'limit': 0}, 'api': {'enabled': False, 'limit': 0}, 'assignments': {'enabled': True, 'limit': 0}, 'courses': {'enabled': True, 'limit': 0}, 'members': {'admin_limit': 1, 'enabled': True, 'limit': 1}, 'usergroups': {'enabled': False, 'limit': 0}, 'live_sessions': {'enabled': False, 'limit': 0}, 'paths': {'enabled': False}, 'messaging': {'enabled': True}, 'communities': {'enabled': False}, 'roles': {'enabled': False}, 'scorm': {'enabled': False}, 'sso': {'enabled': False}, 'lti': {'enabled': False}, 'versioning': {'enabled': False}, 'webhooks': {'enabled': False, 'limit': 0}, 'audit_logs': {'enabled': False}}, 'general': {'watermark': False}, 'cloud': {'plan': 'personal', 'custom_domain': False}}, 'personal-family': {'features': {'analytics': {'enabled': False, 'limit': 0}, 'api': {'enabled': False, 'limit': 0}, 'assignments': {'enabled': True, 'limit': 0}, 'courses': {'enabled': True, 'limit': 0}, 'members': {'admin_limit': 4, 'enabled': True, 'limit': 4}, 'usergroups': {'enabled': False, 'limit': 0}, 'live_sessions': {'enabled': False, 'limit': 0}, 'paths': {'enabled': False}, 'messaging': {'enabled': True}, 'communities': {'enabled': False}, 'roles': {'enabled': False}, 'scorm': {'enabled': False}, 'sso': {'enabled': False}, 'lti': {'enabled': False}, 'versioning': {'enabled': False}, 'webhooks': {'enabled': False, 'limit': 0}, 'audit_logs': {'enabled': False}}, 'general': {'watermark': False}, 'cloud': {'plan': 'personal-family', 'custom_domain': False}}, 'standard': {'features': {'analytics': {'enabled': True, 'limit': 0}, 'api': {'enabled': False, 'limit': 0}, 'assignments': {'enabled': True, 'limit': 0}, 'courses': {'enabled': True, 'limit': 0}, 'members': {'admin_limit': 2, 'enabled': True, 'limit': 200}, 'usergroups': {'enabled': True, 'limit': 0}, 'live_sessions': {'enabled': False, 'limit': 0}, 'paths': {'enabled': False}, 'messaging': {'enabled': True}, 'communities': {'enabled': True}, 'roles': {'enabled': False}, 'scorm': {'enabled': False}, 'sso': {'enabled': False}, 'lti': {'enabled': False}, 'versioning': {'enabled': False}, 'webhooks': {'enabled': False, 'limit': 0}, 'audit_logs': {'enabled': False}}, 'general': {'watermark': False}, 'cloud': {'plan': 'standard', 'custom_domain': False}}, 'pro': {'features': {'analytics': {'enabled': True, 'limit': 0}, 'api': {'enabled': True, 'limit': 0}, 'assignments': {'enabled': True, 'limit': 0}, 'courses': {'enabled': True, 'limit': 0}, 'members': {'admin_limit': 10, 'enabled': True, 'limit': 500}, 'usergroups': {'enabled': True, 'limit': 0}, 'live_sessions': {'enabled': True, 'limit': 0}, 'paths': {'enabled': True}, 'messaging': {'enabled': True}, 'communities': {'enabled': True}, 'roles': {'enabled': True}, 'scorm': {'enabled': False}, 'sso': {'enabled': False}, 'lti': {'enabled': False}, 'versioning': {'enabled': True}, 'webhooks': {'enabled': True, 'limit': 0}, 'audit_logs': {'enabled': False}}, 'general': {'watermark': False}, 'cloud': {'plan': 'pro', 'custom_domain': True}}, 'enterprise': {'features': {'analytics': {'enabled': True, 'limit': 0}, 'api': {'enabled': True, 'limit': 0}, 'assignments': {'enabled': True, 'limit': 0}, 'courses': {'enabled': True, 'limit': 0}, 'members': {'admin_limit': 100, 'enabled': True, 'limit': 0}, 'usergroups': {'enabled': True, 'limit': 0}, 'live_sessions': {'enabled': True, 'limit': 0}, 'paths': {'enabled': True}, 'messaging': {'enabled': True}, 'communities': {'enabled': True}, 'roles': {'enabled': True}, 'scorm': {'enabled': True}, 'sso': {'enabled': True}, 'lti': {'enabled': True}, 'versioning': {'enabled': True}, 'webhooks': {'enabled': True, 'limit': 0}, 'audit_logs': {'enabled': True}}, 'general': {'watermark': False}, 'cloud': {'plan': 'enterprise', 'custom_domain': True}}}
PLAN_LIMITS: dict[str, dict[str, int]] = {plan: {'courses': cfg['features']['courses']['limit'], 'members': cfg['features']['members']['limit'], 'admin_seats': cfg['features']['members']['admin_limit']} for plan, cfg in PLAN_FEATURE_CONFIGS.items()}

def get_plan_feature_config(plan: str, feature: str) -> dict:
    """
    Get the full feature config for a plan.

    Returns:
        Dict with at least {enabled, limit} keys. Returns disabled/0 for unknown features.
    """
    cfg = PLAN_FEATURE_CONFIGS.get(plan, PLAN_FEATURE_CONFIGS['free'])
    return cfg['features'].get(feature, {'enabled': False, 'limit': 0})

def is_paying_plan(plan: str) -> bool:
    """
    Whether `plan` is a paid plan (anything above free).

    Features INCLUDED in a paid plan are guaranteed to the organization: the
    superadmin kill switch can never disable them. It still governs free orgs and
    comp-granted extras. The org's own toggle is separate and always applies.
    See resolve_feature().
    """
    return plan not in ('free',)
_BUSINESS_UPGRADES: list[str] = ['standard', 'pro', 'enterprise']

def get_upgrade_plan(plan: str, feature: str) -> str | None:
    """
    Cheapest plan above `plan` whose config includes `feature`.

    Used to label a locked feature with the plan that actually unlocks it, which
    can differ from FEATURE_PLAN_REQUIREMENTS (e.g. boards are in Personal but not
    in Standard, so a Standard org needs Pro). Returns None when the feature is
    already included or no plan includes it.
    """
    if get_plan_feature_config(plan, feature).get('enabled', False):
        return None
    if plan in ('personal', 'personal-family'):
        start = PLAN_HIERARCHY.index(plan) + 1
        candidates = PLAN_HIERARCHY[start:]
    else:
        current = PLAN_HIERARCHY.index(plan) if plan in PLAN_HIERARCHY else 0
        candidates = [p for p in _BUSINESS_UPGRADES if PLAN_HIERARCHY.index(p) > current]
    for candidate in candidates:
        if get_plan_feature_config(candidate, feature).get('enabled', False):
            return candidate
    return None

def is_feature_enabled_for_plan(plan: str, feature: str) -> bool:
    """Check if a feature is enabled for a given plan."""
    from src.core.deployment_mode import get_deployment_mode
    mode = get_deployment_mode()
    if mode == 'ee':
        return True
    if mode == 'oss':
        return feature not in ('analytics', 'api', 'sso', 'audit_logs', 'scorm', 'lti')
    return get_plan_feature_config(plan, feature).get('enabled', False)

def get_feature_limit_for_plan(plan: str, feature: str) -> int:
    """
    Get the limit for a specific feature from the plan config.

    Returns:
        The limit (0 = unlimited).
    """
    from src.core.deployment_mode import get_deployment_mode
    mode = get_deployment_mode()
    if mode != 'saas':
        return 0
    return get_plan_feature_config(plan, feature).get('limit', 0)

def get_plan_config(plan: str) -> dict:
    """Get the full plan config. Returns free config for unknown plans."""
    return PLAN_FEATURE_CONFIGS.get(plan, PLAN_FEATURE_CONFIGS['free'])


def get_plan_limit(plan: str, feature: str) -> int:
    """
    Get the limit for a plan-based feature (courses, members, admin_seats).

    Returns:
        The limit for the feature (0 means unlimited)
    """
    from src.core.deployment_mode import get_deployment_mode
    mode = get_deployment_mode()
    if mode != 'saas':
        return 0
    plan_limits = PLAN_LIMITS.get(plan, PLAN_LIMITS['free'])
    return plan_limits.get(feature, 0)

def plan_meets_requirement(current_plan: str, required_plan: str) -> bool:
    """
    Check if the current plan meets or exceeds the required plan level.
    """
    from src.core.deployment_mode import get_deployment_mode
    mode = get_deployment_mode()
    if mode == 'ee':
        return True
    if mode == 'oss':
        return required_plan != 'enterprise'
    try:
        current_index = PLAN_HIERARCHY.index(current_plan)
    except ValueError:
        current_index = 0
    try:
        required_index = PLAN_HIERARCHY.index(required_plan)
    except ValueError:
        required_index = 0
    return current_index >= required_index

def get_required_plan_for_feature(feature_key: str) -> PlanLevel | None:
    """
    Get the required plan level for a specific feature.
    """
    return FEATURE_PLAN_REQUIREMENTS.get(feature_key)
