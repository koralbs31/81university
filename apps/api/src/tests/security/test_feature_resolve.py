from unittest.mock import patch
from src.security.features_utils.resolve import _get_admin_toggle, _get_overrides, _get_plan_from_config, resolve_all_features, resolve_feature

class TestFeatureResolve:

    def test_config_extractors_cover_v1_v2_shapes(self):
        v2_config = {'config_version': '2.0', 'plan': 'pro', 'admin_toggles': {'paths': {'disabled': True}}, 'overrides': {'members': {'extra_limit': 7}}}
        v1_config = {'cloud': {'plan': 'standard'}, 'features': {'paths': {'enabled': False, 'signup_mode': 'invite'}}}
        assert _get_plan_from_config(v2_config) == 'pro'
        assert _get_plan_from_config(v1_config) == 'standard'
        assert _get_admin_toggle(v2_config, 'paths') == {'disabled': True}
        assert _get_admin_toggle(v1_config, 'paths') == {'disabled': True, 'signup_mode': 'invite'}
        assert _get_overrides(v2_config, 'members') == {'extra_limit': 7}
        assert _get_overrides(v1_config, 'members') == {}

    def test_always_on_features_and_mode_shortcuts(self):
        with patch('src.security.features_utils.resolve.get_deployment_mode', return_value='oss'):
            assert resolve_feature('usergroups', {}, 0) == {'enabled': True, 'available': True, 'limit': 0, 'required_plan': 'standard', 'disabled_by': None, 'upgrade_plan': None}
        for mode in ('ee', 'oss'):
            with patch('src.security.features_utils.resolve.get_deployment_mode', return_value=mode):
                assert resolve_feature('courses', {}, 0) == {'enabled': True, 'available': True, 'limit': 0, 'required_plan': None, 'disabled_by': None, 'upgrade_plan': None}

    def test_saas_resolution_covers_overrides_limits_and_admin_toggles(self):
        config = {'config_version': '2.0', 'plan': 'free', 'admin_toggles': {'members': {'disabled': False}, 'paths': {'disabled': True}}, 'overrides': {'members': {'extra_limit': 3}, 'analytics': {'force_enabled': True}}}
        with patch('src.security.features_utils.resolve.get_deployment_mode', return_value='saas'):
            members = resolve_feature('members', config, org_id=12)
            analytics = resolve_feature('analytics', config, org_id=12)
            boards = resolve_feature('paths', config, org_id=12)
        assert members == {'enabled': True, 'available': True, 'limit': 13, 'required_plan': None, 'disabled_by': None, 'upgrade_plan': None}
        assert analytics == {'enabled': True, 'available': True, 'limit': 0, 'required_plan': 'standard', 'disabled_by': None, 'upgrade_plan': None}
        assert boards == {'enabled': False, 'available': False, 'limit': 0, 'required_plan': 'pro', 'disabled_by': None, 'upgrade_plan': 'pro'}

    def test_courses_saas_limit_resolution_uses_plan_overrides_and_purchased_extra(self):
        config = {'config_version': '2.0', 'plan': 'standard'}
        with patch('src.security.features_utils.resolve.get_deployment_mode', return_value='saas'), patch('src.security.features_utils.resolve.get_plan_feature_config', side_effect=[{'limit': 0}, {'limit': 5}]), patch('src.security.features_utils.resolve._get_overrides', return_value={'extra_limit': 2}):
            unlimited = resolve_feature('courses', config, org_id=12)
            limited = resolve_feature('courses', config, org_id=12)
        assert unlimited == {'enabled': True, 'available': True, 'limit': 0, 'required_plan': None, 'disabled_by': None, 'upgrade_plan': None}
        assert limited == {'enabled': True, 'available': True, 'limit': 7, 'required_plan': None, 'disabled_by': None, 'upgrade_plan': None}

    def test_ee_and_oss_mode_shortcuts_cover_admin_toggle_and_ee_only_blocks(self):
        config = {'config_version': '2.0', 'admin_toggles': {'analytics': {'disabled': True}, 'paths': {'disabled': False}}}
        config_allowed = {'config_version': '2.0', 'admin_toggles': {'analytics': {'disabled': False}}}
        with patch('src.security.features_utils.resolve.get_deployment_mode', return_value='ee'):
            ee_disabled = resolve_feature('analytics', config, org_id=0)
        with patch('src.security.features_utils.resolve.get_deployment_mode', return_value='oss'):
            oss_blocked = resolve_feature('sso', config, org_id=0)
            oss_allowed = resolve_feature('analytics', config_allowed, org_id=0)
        assert ee_disabled == {'enabled': False, 'available': True, 'limit': 0, 'required_plan': 'standard', 'disabled_by': 'platform', 'upgrade_plan': None}
        assert oss_blocked == {'enabled': False, 'available': False, 'limit': 0, 'required_plan': 'enterprise', 'disabled_by': None, 'upgrade_plan': 'enterprise'}
        assert oss_allowed == {'enabled': True, 'available': True, 'limit': 0, 'required_plan': 'standard', 'disabled_by': None, 'upgrade_plan': None}

    def test_paid_plan_feature_cannot_be_admin_disabled(self):
        base = {'config_version': '2.0', 'plan': 'pro'}
        toggled_off = {**base, 'admin_toggles': {'roles': {'disabled': True}}}
        with patch('src.security.features_utils.resolve.get_deployment_mode', return_value='saas'):
            without_toggle = resolve_feature('roles', base, org_id=0)
            with_toggle = resolve_feature('roles', toggled_off, org_id=0)
        assert without_toggle['available'] is True
        assert without_toggle['enabled'] is True
        assert with_toggle['available'] is True
        assert with_toggle['enabled'] is True

    def test_free_plan_feature_can_be_admin_disabled(self):
        config = {'config_version': '2.0', 'plan': 'free', 'admin_toggles': {'members': {'disabled': True}}}
        with patch('src.security.features_utils.resolve.get_deployment_mode', return_value='saas'):
            members = resolve_feature('members', config, org_id=0)
        assert members['available'] is True
        assert members['enabled'] is False

    def test_org_toggle_turns_off_a_paid_plan_feature(self):
        config = {'config_version': '2.0', 'plan': 'pro', 'admin_toggles': {'paths': {'org_disabled': True, 'disabled': True}}}
        with patch('src.security.features_utils.resolve.get_deployment_mode', return_value='saas'):
            boards = resolve_feature('paths', config, org_id=0)
        assert boards['available'] is True
        assert boards['enabled'] is False
        assert boards['disabled_by'] == 'org'
        assert boards['upgrade_plan'] is None

    def test_platform_toggle_reported_on_free_plan(self):
        config = {'config_version': '2.0', 'plan': 'free', 'admin_toggles': {'members': {'disabled': True}}}
        with patch('src.security.features_utils.resolve.get_deployment_mode', return_value='saas'):
            members = resolve_feature('members', config, org_id=0)
        assert members['disabled_by'] == 'platform'

    def test_org_toggle_in_self_hosted_modes(self):
        config = {'config_version': '2.0', 'admin_toggles': {'podcasts': {'org_disabled': True}}}
        for mode in ('ee', 'oss'):
            with patch('src.security.features_utils.resolve.get_deployment_mode', return_value=mode):
                podcasts = resolve_feature('podcasts', config, org_id=0)
            assert podcasts['enabled'] is False
            assert podcasts['disabled_by'] == 'org'

    def test_locked_feature_reports_the_plan_that_unlocks_it(self):
        with patch('src.security.features_utils.resolve.get_deployment_mode', return_value='saas'):
            standard = {'config_version': '2.0', 'plan': 'standard'}
            assert resolve_feature('paths', standard, org_id=0)['upgrade_plan'] == 'pro'
            assert resolve_feature('live_sessions', standard, org_id=0)['upgrade_plan'] == 'pro'
            assert resolve_feature('communities', standard, org_id=0)['upgrade_plan'] is None
            free = {'config_version': '2.0', 'plan': 'free'}
            assert resolve_feature('communities', free, org_id=0)['upgrade_plan'] == 'standard'
            personal = {'config_version': '2.0', 'plan': 'personal'}
            assert resolve_feature('paths', personal, org_id=0)['upgrade_plan'] == 'pro'

    def test_upgrade_plan_edges(self):
        from src.security.features_utils.plans import get_upgrade_plan
        assert get_upgrade_plan('pro', 'paths') is None
        assert get_upgrade_plan('free', 'not_a_feature') is None
        assert get_upgrade_plan('oss', 'communities') == 'standard'
        assert get_upgrade_plan('personal-family', 'sso') == 'enterprise'

    def test_resolve_all_features_uses_resolve_feature_for_every_entry(self):
        calls = []

        def _fake_resolve(feature: str, config: dict, org_id: int=0, _extras=None):
            calls.append((feature, config, org_id))
            return {'enabled': True, 'limit': 1, 'required_plan': None}
        with patch('src.security.features_utils.resolve.resolve_feature', side_effect=_fake_resolve):
            result = resolve_all_features({'config_version': '2.0'}, org_id=9)
        assert list(result) == ['analytics', 'api', 'assignments', 'audit_logs', 'communities', 'courses', 'live_sessions', 'lti', 'members', 'messaging', 'paths', 'roles', 'scorm', 'sso', 'usergroups', 'versioning']
        assert len(calls) == 16
        assert calls[0] == ('analytics', {'config_version': '2.0'}, 9)
