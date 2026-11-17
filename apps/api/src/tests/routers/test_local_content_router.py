"""Router tests for src/routers/local_content.py."""
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from src.core.events.database import get_db_session
from src.db.courses.courses import Course
from src.tests.fixtures.rows import activity_row
from src.db.user_organizations import UserOrganization
from src.db.users import AnonymousUser
from src.routers.local_content import router as local_content_router
from src.security.auth import get_current_user

@pytest.fixture
def app(db, admin_user):
    app = FastAPI()
    app.include_router(local_content_router)
    app.dependency_overrides[get_db_session] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: admin_user
    yield app
    app.dependency_overrides.clear()

@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as c:
        yield c

class TestLocalContentRouter:

    async def test_get_and_head_local_content_success(self, client, db, org, course, admin_user, app, tmp_path):
        content_root = tmp_path / 'content'
        file_path = content_root / 'orgs' / org.org_uuid / 'courses' / course.course_uuid / 'thumb.png'
        file_path.parent.mkdir(parents=True)
        file_path.write_bytes(b'png')
        from src.routers import local_content
        original_dir = local_content.CONTENT_DIR
        local_content.CONTENT_DIR = content_root
        try:
            get_response = await client.get(f'/content/orgs/{org.org_uuid}/courses/{course.course_uuid}/thumb.png')
            head_response = await client.head(f'/content/orgs/{org.org_uuid}/courses/{course.course_uuid}/thumb.png')
        finally:
            local_content.CONTENT_DIR = original_dir
        assert get_response.status_code == 200
        assert get_response.content == b'png'
        assert head_response.status_code == 200
        assert head_response.headers['content-type'] == 'image/png'
        assert head_response.headers['content-length'] == '3'

    async def test_private_course_activity_requires_membership(self, client, db, org, regular_user, anonymous_user, app, tmp_path):
        course = Course(id=10, name='Private Course', description='Desc', public=False, published=True, open_to_contributors=False, org_id=org.id, course_uuid='course_private_local', creation_date='2024-01-01', update_date='2024-01-01')
        db.add(course)
        db.add(activity_row(10, org.id, 10, 'activity_x'))
        await db.commit()
        content_root = tmp_path / 'content'
        file_path = content_root / 'orgs' / org.org_uuid / 'courses' / course.course_uuid / 'activities' / 'activity_x' / 'video.mp4'
        file_path.parent.mkdir(parents=True)
        file_path.write_bytes(b'video')
        from src.routers import local_content
        original_dir = local_content.CONTENT_DIR
        local_content.CONTENT_DIR = content_root
        try:
            app.dependency_overrides[get_current_user] = lambda: AnonymousUser()
            anon_response = await client.get(f'/content/orgs/{org.org_uuid}/courses/{course.course_uuid}/activities/activity_x/video.mp4')
            app.dependency_overrides[get_current_user] = lambda: regular_user.model_copy(update={'id': 222})
            forbidden_response = await client.get(f'/content/orgs/{org.org_uuid}/courses/{course.course_uuid}/activities/activity_x/video.mp4')
            db.add(UserOrganization(user_id=regular_user.id, org_id=org.id, role_id=1, creation_date='2024-01-01', update_date='2024-01-01'))
            await db.commit()
            app.dependency_overrides[get_current_user] = lambda: regular_user
            ok_response = await client.get(f'/content/orgs/{org.org_uuid}/courses/{course.course_uuid}/activities/activity_x/video.mp4')
        finally:
            local_content.CONTENT_DIR = original_dir
        assert anon_response.status_code == 401
        assert forbidden_response.status_code == 403
        assert ok_response.status_code == 200
        assert ok_response.headers['content-type'] == 'video/mp4'

    async def test_dot_segment_cannot_bypass_the_access_check(self, client, db, org, regular_user, app, tmp_path):
        course = Course(id=11, name='Private Course', description='Desc', public=False, published=True, open_to_contributors=False, org_id=org.id, course_uuid='course_dotseg', creation_date='2024-01-01', update_date='2024-01-01')
        db.add(course)
        db.add(activity_row(11, org.id, 11, 'activity_x'))
        await db.commit()
        content_root = tmp_path / 'content'
        file_path = content_root / 'orgs' / org.org_uuid / 'courses' / course.course_uuid / 'activities' / 'activity_x' / 'secret.mp4'
        file_path.parent.mkdir(parents=True)
        file_path.write_bytes(b'secret')
        from src.routers import local_content
        original_dir = local_content.CONTENT_DIR
        local_content.CONTENT_DIR = content_root
        try:
            app.dependency_overrides[get_current_user] = lambda: AnonymousUser()
            sneaky = await client.get(f'/content/orgs/./{org.org_uuid}/courses/{course.course_uuid}/activities/activity_x/secret.mp4')
            sneaky_encoded = await client.get(f'/content/orgs/%252e/{org.org_uuid}/courses/{course.course_uuid}/activities/activity_x/secret.mp4')
        finally:
            local_content.CONTENT_DIR = original_dir
        assert sneaky.status_code in (400, 401)
        assert sneaky_encoded.status_code in (400, 401)
    pass
    pass

class TestLocalContentPathCase:

    @pytest.mark.parametrize('variant', ['subs', 'SUBS', 'Courses', 'ACTIVITIES', 'ſubs'])
    async def test_submission_file_not_served_to_anonymous_under_any_casing(self, client, app, db, org, course, activity, anonymous_user, tmp_path, variant):
        content_root = tmp_path / 'content'
        sub_dir = content_root / 'orgs' / org.org_uuid / 'courses' / course.course_uuid / 'activities' / activity.activity_uuid / 'assignments' / 'assignment_x' / 'tasks' / 'task_x' / 'subs'
        sub_dir.mkdir(parents=True)
        (sub_dir / 'work.txt').write_text('private')
        path = f'orgs/{org.org_uuid}/courses/{course.course_uuid}/activities/{activity.activity_uuid}/assignments/assignment_x/tasks/task_x/subs/work.txt'
        if variant != 'subs':
            path = path.replace(f"/{variant.lower().replace('ſ', 's')}/", f'/{variant}/')
        from src.routers import local_content
        app.dependency_overrides[get_current_user] = lambda: anonymous_user
        original_dir = local_content.CONTENT_DIR
        local_content.CONTENT_DIR = content_root
        try:
            response = await client.get(f'/content/{path}')
        finally:
            local_content.CONTENT_DIR = original_dir
        assert response.status_code != 200
        assert b'private' not in response.content
