"""Router tests for src/routers/paths/paths.py and the Paths plan gate.

Mounts the routers the way src/router.py does: certificate verification first
and outside the plan gate, the rest behind ``require_plan_for_paths``.
"""
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode
import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request
from src.core.events.database import get_db_session
from src.db.organization_config import OrganizationConfig
from src.db.paths import Path, PathCertificate
from src.routers.paths.paths import certificates_router, router
from src.security.api_token_utils import get_authenticated_non_api_token_user
from src.security.auth import get_current_user
from src.security.features_utils.plan_check import require_plan_for_paths

@pytest.fixture(autouse=True)
def _no_webhooks():
    with patch('src.services.paths.paths.dispatch_webhooks', new_callable=AsyncMock):
        yield

@pytest.fixture
def app(db, admin_user):
    app = FastAPI()
    app.include_router(certificates_router, prefix='/api/v1/paths')
    app.include_router(router, prefix='/api/v1/paths', dependencies=[Depends(require_plan_for_paths('pro', 'Paths'))])
    app.dependency_overrides[get_db_session] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: admin_user
    app.dependency_overrides[get_authenticated_non_api_token_user] = lambda: admin_user
    yield app
    app.dependency_overrides.clear()

@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as c:
        yield c

async def _plan(db, org, plan: str):
    db.add(OrganizationConfig(org_id=org.id, config={'config_version': '2.0', 'plan': plan}, creation_date=str(datetime.now()), update_date=str(datetime.now())))
    await db.commit()

async def _saved_path(db, org, **fields) -> Path:
    path = Path(name='Path', org_id=org.id, path_uuid='path_router', published=True, public=True, creation_date='2026-01-01', update_date='2026-01-01', **fields)
    db.add(path)
    await db.commit()
    await db.refresh(path)
    return path

@contextmanager
def _mode(mode: str):
    with patch('src.security.features_utils.plan_check.get_deployment_mode', return_value=mode), patch('src.core.deployment_mode.get_deployment_mode', return_value=mode):
        yield

def _request(path_params=None, query_params=None):
    return Request({'type': 'http', 'method': 'GET', 'path': '/', 'headers': [], 'query_string': urlencode(query_params or {}).encode(), 'path_params': path_params or {}})

class TestRoutes:

    async def test_crud_round_trip(self, client, org, course):
        res = await client.post('/api/v1/paths/', json={'org_id': org.id, 'name': 'Analyst'})
        assert res.status_code == 200, res.text
        uuid = res.json()['path_uuid']
        res = await client.put(f'/api/v1/paths/{uuid}/courses', json={'courses': [{'course_uuid': course.course_uuid, 'required': True}]})
        assert res.status_code == 200
        assert res.json()['courses'][0]['course']['name'] == 'Test Course'
        res = await client.put(f'/api/v1/paths/{uuid}', json={'published': True, 'level': 'beginner'})
        assert res.json()['published'] is True
        assert [p['path_uuid'] for p in (await client.get(f'/api/v1/paths/org/{org.id}')).json()] == [uuid]
        assert (await client.get(f'/api/v1/paths/course/{course.course_uuid}')).json()[0]['path_uuid'] == uuid
        res = await client.post(f'/api/v1/paths/{uuid}/enroll')
        assert res.json()['progress']['enrolled'] is True
        assert len((await client.get(f'/api/v1/paths/me/org/{org.id}')).json()) == 1
        assert len((await client.get(f'/api/v1/paths/{uuid}/learners')).json()) == 1
        assert (await client.delete(f'/api/v1/paths/{uuid}/enroll')).status_code == 200
        assert (await client.delete(f'/api/v1/paths/{uuid}')).status_code == 200
        assert (await client.get(f'/api/v1/paths/{uuid}')).status_code == 404

    async def test_upload_thumbnail_route(self, client, db, org):
        path = await _saved_path(db, org)
        with patch('src.services.utils.upload_content.upload_file', new_callable=AsyncMock, return_value='cover.png'):
            res = await client.put(f'/api/v1/paths/{path.path_uuid}/thumbnail', files={'thumbnail': ('cover.png', b'\x89PNG', 'image/png')})
        assert res.status_code == 200 and res.json()['thumbnail_image'] == 'cover.png'

    async def test_certificate_routes_are_not_read_as_a_path(self, client, db, org, admin_user):
        path = await _saved_path(db, org)
        db.add(PathCertificate(path_id=path.id, user_id=admin_user.id, certificate_uuid='PATH-1', created_at='x'))
        await db.commit()
        mine = await client.get('/api/v1/paths/certificates/me')
        assert mine.status_code == 200 and mine.json()[0]['certificate']['certificate_uuid'] == 'PATH-1'
        assert (await client.get('/api/v1/paths/certificates/PATH-1')).json()['path']['name'] == 'Path'
        assert (await client.get('/api/v1/paths/certificates/nope')).status_code == 404
        res = await client.delete(f'/api/v1/paths/{path.path_uuid}/certificates/PATH-1')
        assert res.status_code == 200
        assert (await client.get('/api/v1/paths/certificates/PATH-1')).status_code == 404
        res = await client.post(f'/api/v1/paths/{path.path_uuid}/certificates/PATH-1/reinstate')
        assert res.status_code == 200
        assert (await client.get('/api/v1/paths/certificates/PATH-1')).status_code == 200

class TestPlanGate:

    async def test_oss_bypasses(self, db):
        dep = require_plan_for_paths('pro', 'Paths')
        with _mode('oss'):
            assert await dep(_request(), db) is True
