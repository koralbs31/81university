"""
Central feature resolution logic (v2 config).

4-layer resolution: deployment mode → plan config → overrides → purchased packs → admin toggles.
"""
from src.core.deployment_mode import get_deployment_mode, EE_ONLY_FEATURES
from src.security.features_utils.plans import FEATURE_PLAN_REQUIREMENTS, get_plan_feature_config, get_upgrade_plan, is_paying_plan
ORG_TOGGLEABLE_FEATURES = {'communities', 'live_sessions', 'paths'}
ALWAYS_ON_FEATURES = {'courses', 'usergroups', 'assignments'}
ALWAYS_ON_WITH_LIMITS = {'courses', 'assignments'}
ALL_FEATURES = ['analytics', 'api', 'assignments', 'audit_logs', 'communities', 'courses', 'live_sessions', 'lti', 'members', 'messaging', 'paths', 'roles', 'scorm', 'sso', 'usergroups', 'versioning']

def _get_plan_from_config(config: dict) -> str:
    """Extract plan from config, supporting both v1 and v2 formats."""
    version = config.get('config_version', '1.0')
    if version.startswith('2'):
        return config.get('plan', 'free')
    return config.get('cloud', {}).get('plan', 'free')

def _get_admin_toggle(config: dict, feature: str) -> dict:
    """Get admin toggle for a feature, supporting both v1 and v2 formats."""
    version = config.get('config_version', '1.0')
    if version.startswith('2'):
        return config.get('admin_toggles', {}).get(feature, {})
    v1_feature = config.get('features', {}).get(feature, {})
    toggle = {}
    if 'enabled' in v1_feature:
        toggle['disabled'] = not v1_feature['enabled']
    if 'signup_mode' in v1_feature:
        toggle['signup_mode'] = v1_feature['signup_mode']
    return toggle

def _get_overrides(config: dict, feature: str) -> dict:
    """Get overrides for a feature from v2 config."""
    version = config.get('config_version', '1.0')
    if not version.startswith('2'):
        return {}
    return config.get('overrides', {}).get(feature, {})

def _state(enabled: bool, available: bool, limit: int, required_plan: str | None, disabled_by: str | None=None, upgrade_plan: str | None=None) -> dict:
    return {'enabled': enabled, 'available': available, 'limit': limit, 'required_plan': required_plan, 'disabled_by': disabled_by, 'upgrade_plan': upgrade_plan}

def _toggle_off_reason(admin_toggle: dict, platform_applies: bool=True) -> str | None:
    """Who turned an available feature off: the org itself, the platform, or nobody."""
    if admin_toggle.get('org_disabled', False):
        return 'org'
    if platform_applies and admin_toggle.get('disabled', False):
        return 'platform'
    return None

def resolve_feature(feature: str, config: dict, org_id: int=0, _extras: dict | None=None) -> dict:
    """
    Resolve a single feature's state through all layers.

    Returns:
        {"enabled", "available", "limit", "required_plan", "disabled_by", "upgrade_plan"}

    - "available" = the feature is part of the org's PLAN entitlement (what the
      plan makes usable), independent of any toggle.
    - "enabled" = whether it is actually ON after the toggles.
    - "disabled_by" = why an available feature is off: "org" (the org turned it
      off in its Features settings) or "platform" (superadmin kill switch). None
      when enabled or when it is simply not in the plan.
    - "upgrade_plan" = the cheapest plan that includes the feature when it is not
      available, so the UI can label it with the plan that actually unlocks it.
    - limit=0 means unlimited.

    Two toggles live under admin_toggles[feature]:
    - "org_disabled": the org's own choice. Always honored.
    - "disabled": the superadmin kill switch. It can never take away a feature a
      PAID plan includes; it still governs free orgs and comp-granted extras.
    """
    mode = get_deployment_mode()
    required_plan = FEATURE_PLAN_REQUIREMENTS.get(feature)
    if feature in ALWAYS_ON_FEATURES and feature not in ALWAYS_ON_WITH_LIMITS:
        return _state(True, True, 0, required_plan)
    if feature in ALWAYS_ON_WITH_LIMITS:
        if mode in ('ee', 'oss'):
            return _state(True, True, 0, required_plan)
        plan = _get_plan_from_config(config)
        plan_config = get_plan_feature_config(plan, feature)
        plan_limit = plan_config.get('limit', 0)
        overrides = _get_overrides(config, feature)
        extra_limit = overrides.get('extra_limit', 0)
        if plan_limit == 0:
            effective_limit = 0
        else:
            effective_limit = plan_limit + extra_limit
        return _state(True, True, effective_limit, required_plan)
    admin_toggle = _get_admin_toggle(config, feature)
    if mode == 'ee':
        off_by = _toggle_off_reason(admin_toggle)
        return _state(off_by is None, True, 0, required_plan, off_by)
    if mode == 'oss':
        if feature in EE_ONLY_FEATURES:
            return _state(False, False, 0, required_plan, upgrade_plan='enterprise')
        off_by = _toggle_off_reason(admin_toggle)
        return _state(off_by is None, True, 0, required_plan, off_by)
    plan = _get_plan_from_config(config)
    plan_config = get_plan_feature_config(plan, feature)
    plan_enabled = plan_config.get('enabled', False)
    plan_limit = plan_config.get('limit', 0)
    overrides = _get_overrides(config, feature)
    force_enabled = overrides.get('force_enabled', False)
    extra_limit = overrides.get('extra_limit', 0)
    available = plan_enabled or force_enabled
    if plan_limit == 0:
        effective_limit = 0
    else:
        effective_limit = plan_limit + extra_limit
    if not available:
        return _state(False, False, effective_limit, required_plan, upgrade_plan=get_upgrade_plan(plan, feature))
    plan_guaranteed = plan_enabled and is_paying_plan(plan)
    off_by = _toggle_off_reason(admin_toggle, platform_applies=not plan_guaranteed)
    return _state(off_by is None, True, effective_limit, required_plan, off_by)

def resolve_all_features(config: dict, org_id: int=0) -> dict:
    """Resolve all features for an organization config."""
    return {feature: resolve_feature(feature, config, org_id) for feature in ALL_FEATURES}
