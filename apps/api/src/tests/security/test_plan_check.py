"""Tests for src/security/features_utils/plan_check.py."""
from datetime import datetime
from unittest.mock import patch
from urllib.parse import urlencode
import pytest
from fastapi import HTTPException
from starlette.requests import Request
from src.db.courses.certifications import CertificateUser, Certifications
from src.db.organization_config import OrganizationConfig
from src.db.usergroups import UserGroup
from src.security.features_utils.plan_check import _check_mode_bypass, get_org_plan, require_plan, require_plan_for_certifications, require_plan_for_usergroups

def _request(path_params=None, query_params=None):
    query_string = urlencode(query_params or {}).encode()
    return Request({'type': 'http', 'method': 'GET', 'path': '/', 'headers': [], 'query_string': query_string, 'path_params': path_params or {}})

async def _make_org_config(db, org, config):
    org_config = OrganizationConfig(org_id=org.id, config=config, creation_date=str(datetime.now()), update_date=str(datetime.now()))
    db.add(org_config)
    await db.commit()
    await db.refresh(org_config)
    return org_config

class TestPlanCheck:

    def test_check_mode_bypass(self):
        with patch('src.security.features_utils.plan_check.get_deployment_mode', return_value='ee'):
            assert _check_mode_bypass('Boards') is True
        with patch('src.security.features_utils.plan_check.get_deployment_mode', return_value='saas'):
            assert _check_mode_bypass('Boards') is None
        with patch('src.security.features_utils.plan_check.get_deployment_mode', return_value='oss'), patch('src.security.features_utils.plan_check.EE_ONLY_FEATURES', {'boards'}):
            with pytest.raises(HTTPException) as exc:
                _check_mode_bypass('Boards')
        assert exc.value.status_code == 403
        with patch('src.security.features_utils.plan_check.get_deployment_mode', return_value='oss'), patch('src.security.features_utils.plan_check.EE_ONLY_FEATURES', {'boards'}):
            assert _check_mode_bypass('Analytics') is True

    @pytest.mark.asyncio
    async def test_get_org_plan_versions_and_missing_config(self, db, org):
        from sqlalchemy import delete as sa_delete
        await _make_org_config(db, org, {'config_version': '1.4', 'cloud': {'plan': 'pro'}})
        assert await get_org_plan(org.id, db) == 'pro'
        await db.execute(sa_delete(OrganizationConfig).where(OrganizationConfig.org_id == org.id))
        await db.commit()
        await _make_org_config(db, org, {'config_version': '2.0', 'plan': 'enterprise'})
        assert await get_org_plan(org.id, db) == 'enterprise'
        await db.execute(sa_delete(OrganizationConfig).where(OrganizationConfig.org_id == org.id))
        await db.commit()
        with pytest.raises(HTTPException) as exc:
            await get_org_plan(org.id, db)
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_require_plan_from_path_and_query(self, db, org):
        await _make_org_config(db, org, {'config_version': '2.0', 'plan': 'standard'})
        dependency = require_plan('pro', 'Analytics')
        with patch('src.security.features_utils.plan_check.get_deployment_mode', return_value='saas'), patch('src.security.features_utils.plan_check.plan_meets_requirement', side_effect=[False, True]):
            with pytest.raises(HTTPException) as low_plan_exc:
                await dependency(_request(path_params={'org_id': str(org.id)}), db)
            assert await dependency(_request(query_params={'org_id': str(org.id)}), db) is True
            with pytest.raises(HTTPException) as missing_org_exc:
                await dependency(_request(path_params={'org_id': 'abc'}), db)
            with pytest.raises(HTTPException) as invalid_query_exc:
                await dependency(_request(query_params={'org_id': 'abc'}), db)
        assert low_plan_exc.value.status_code == 403
        assert missing_org_exc.value.status_code == 400
        assert invalid_query_exc.value.status_code == 400

    pass

    @pytest.mark.asyncio
    async def test_require_plan_for_usergroups_success_and_no_org(self, db, org):
        await _make_org_config(db, org, {'config_version': '2.0', 'plan': 'standard'})
        usergroup = UserGroup(org_id=org.id, name='Group', description='Desc', usergroup_uuid='ug_plan', creation_date=str(datetime.now()), update_date=str(datetime.now()))
        db.add(usergroup)
        await db.commit()
        await db.refresh(usergroup)
        dependency = require_plan_for_usergroups('pro', 'Usergroups')
        with patch('src.security.features_utils.plan_check.get_deployment_mode', return_value='saas'), patch('src.security.features_utils.plan_check.plan_meets_requirement', return_value=True):
            assert await dependency(_request(), db) is True
            assert await dependency(_request(path_params={'usergroup_id': str(usergroup.id)}), db) is True
        with patch('src.security.features_utils.plan_check.get_deployment_mode', return_value='saas'), patch('src.security.features_utils.plan_check.plan_meets_requirement', return_value=False):
            with pytest.raises(HTTPException) as exc:
                await dependency(_request(path_params={'usergroup_id': str(usergroup.id)}), db)
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_require_plan_for_certifications_variants(self, db, org, course, regular_user):
        await _make_org_config(db, org, {'config_version': '2.0', 'plan': 'enterprise'})
        cert = Certifications(certification_uuid='cert_test', course_id=course.id, config={}, creation_date=str(datetime.now()), update_date=str(datetime.now()))
        db.add(cert)
        await db.commit()
        await db.refresh(cert)
        user_cert = CertificateUser(user_id=regular_user.id, certification_id=cert.id, user_certification_uuid='user_cert_test', created_at=str(datetime.now()), updated_at=str(datetime.now()))
        db.add(user_cert)
        await db.commit()
        dependency = require_plan_for_certifications('pro', 'Certificates')
        with patch('src.security.features_utils.plan_check.get_deployment_mode', return_value='saas'), patch('src.security.features_utils.plan_check.plan_meets_requirement', return_value=True):
            assert await dependency(_request(), db) is True
            assert await dependency(_request(path_params={'certification_uuid': cert.certification_uuid}), db) is True
            assert await dependency(_request(path_params={'course_uuid': course.course_uuid}), db) is True
            assert await dependency(_request(path_params={'user_certification_uuid': user_cert.user_certification_uuid}), db) is True
        with patch('src.security.features_utils.plan_check.get_deployment_mode', return_value='saas'), patch('src.security.features_utils.plan_check.plan_meets_requirement', return_value=False):
            with pytest.raises(HTTPException) as exc:
                await dependency(_request(path_params={'certification_uuid': cert.certification_uuid}), db)
        assert exc.value.status_code == 403
    pass
