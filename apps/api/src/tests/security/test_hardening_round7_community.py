"""
Folders, communities, podcasts and search access fixes:

- a sub-folder (created or moved) needs the right to edit its new parent, and
  pinning to the library root needs the org-wide folder update right;
- authors edit their posts and comments only while they can still post there
  and the thread is open (moderators excepted);
- the Communities plan gate also covers routes addressed by a discussion,
  comment or course uuid;
- editing podcasts and episodes goes through the same plan gate as creating;
- UserGroup members don't get unpublished podcasts in listings;
- search applies folder UserGroup restrictions, and API tokens search through
  their own rights buckets;
- a community's moderation word list only goes to its moderators.
"""
from datetime import datetime
from io import BytesIO
from unittest.mock import AsyncMock, patch
import pytest
from fastapi import HTTPException, UploadFile
from sqlmodel import select
from starlette.requests import Request
from src.db.communities.communities import Community
from src.db.communities.discussion_comments import DiscussionComment, DiscussionCommentUpdate
from src.db.communities.discussions import Discussion, DiscussionUpdate
from src.db.resource_authors import ResourceAuthor, ResourceAuthorshipEnum, ResourceAuthorshipStatusEnum
from src.db.roles import Role, RoleTypeEnum
from src.db.user_organizations import UserOrganization
from src.db.usergroup_resources import UserGroupResource
from src.db.usergroup_user import UserGroupUser
from src.db.usergroups import UserGroup
from src.db.users import APITokenUser, PublicUser, User
from src.routers.communities import discussions as discussions_router
from src.services.communities import comments as comments_service
from src.services.communities import communities as communities_service
from src.services.communities import discussions as discussions_service
from src.services.search import search as search_service
from src.tests.conftest import USER_RIGHTS
NOW = str(datetime.now())

def _req(path_params: dict | None=None) -> Request:
    return Request({'type': 'http', 'method': 'GET', 'path': '/', 'headers': [], 'query_string': b'', 'path_params': path_params or {}})

def _rights(**overrides) -> dict:
    rights = USER_RIGHTS.model_dump()
    rights.update(overrides)
    return rights

async def _role(db, org, *, id: int, name: str, rights: dict) -> Role:
    role = Role(id=id, name=name, org_id=org.id, role_type=RoleTypeEnum.TYPE_ORGANIZATION, role_uuid=f'role_{name}', rights=rights, creation_date=NOW, update_date=NOW)
    db.add(role)
    await db.commit()
    return role

async def _user(db, org, *, id: int, name: str, role_id: int) -> PublicUser:
    db.add(User(id=id, username=name, first_name=name, last_name='User', email=f'{name}@test.com', password='x', user_uuid=f'user_{name}', creation_date=NOW, update_date=NOW))
    db.add(UserOrganization(user_id=id, org_id=org.id, role_id=role_id, creation_date=NOW, update_date=NOW))
    await db.commit()
    return PublicUser(id=id, username=name, first_name=name, last_name='User', email=f'{name}@test.com', user_uuid=f'user_{name}')

async def _author(db, resource_uuid: str, user_id: int) -> None:
    db.add(ResourceAuthor(resource_uuid=resource_uuid, user_id=user_id, authorship=ResourceAuthorshipEnum.CREATOR, authorship_status=ResourceAuthorshipStatusEnum.ACTIVE, creation_date=NOW, update_date=NOW))
    await db.commit()

async def _usergroup(db, org, *, id: int, resource_uuid: str, member_ids=()) -> None:
    db.add(UserGroup(id=id, org_id=org.id, name=f'group{id}', description='', usergroup_uuid=f'usergroup_{id}', creation_date=NOW, update_date=NOW))
    db.add(UserGroupResource(usergroup_id=id, resource_uuid=resource_uuid, org_id=org.id, creation_date=NOW, update_date=NOW))
    for uid in member_ids:
        db.add(UserGroupUser(usergroup_id=id, user_id=uid, org_id=org.id, creation_date=NOW, update_date=NOW))
    await db.commit()

def _token(org, **rights) -> APITokenUser:
    return APITokenUser(org_id=org.id, created_by_user_id=1, rights=rights)
_READ = {'action_read': True}
_DENY = HTTPException(status_code=403, detail='plan')

@pytest.fixture
async def thread(db, org, admin_user):
    poster_rights = _rights(discussions={**USER_RIGHTS.discussions.model_dump(), 'action_create': True})
    await _role(db, org, id=5, name='poster', rights=poster_rights)
    poster = await _user(db, org, id=20, name='poster', role_id=5)
    community = Community(id=70, org_id=org.id, name='c', description='', public=True, community_uuid='community_thread', creation_date=NOW, update_date=NOW)
    db.add(community)
    await db.commit()
    discussion = Discussion(id=71, title='t', content='c', label='general', community_id=community.id, org_id=org.id, author_id=poster.id, discussion_uuid='discussion_thread', creation_date=NOW, update_date=NOW)
    db.add(discussion)
    await db.commit()
    for cid, author in ((72, poster.id), (73, admin_user.id)):
        db.add(DiscussionComment(id=cid, discussion_id=discussion.id, author_id=author, content='x', comment_uuid=f'comment_{cid}', creation_date=NOW, update_date=NOW))
    await db.commit()
    return (poster, discussion)

async def _lock(db, discussion):
    discussion.is_locked = True
    db.add(discussion)
    await db.commit()

async def _remove_from_org(db, user_id):
    row = (await db.execute(select(UserOrganization).where(UserOrganization.user_id == user_id))).scalars().first()
    await db.delete(row)
    await db.commit()

async def test_author_edits_while_still_a_participant(db, thread):
    poster, discussion = thread
    updated = await discussions_service.update_discussion(_req(), discussion.discussion_uuid, DiscussionUpdate(title='new'), poster, db)
    assert updated.title == 'new'
    comment = await comments_service.update_comment(_req(), 'comment_72', DiscussionCommentUpdate(content='edited'), poster, db)
    assert comment.content == 'edited'

async def test_locked_thread_blocks_author_edits(db, thread):
    poster, discussion = thread
    await _lock(db, discussion)
    with pytest.raises(HTTPException) as exc:
        await discussions_service.update_discussion(_req(), discussion.discussion_uuid, DiscussionUpdate(title='new'), poster, db)
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as exc:
        await comments_service.update_comment(_req(), 'comment_72', DiscussionCommentUpdate(content='edited'), poster, db)
    assert exc.value.status_code == 403

async def test_moderator_edits_in_locked_thread(db, thread, admin_user):
    _, discussion = thread
    await _lock(db, discussion)
    updated = await discussions_service.update_discussion(_req(), discussion.discussion_uuid, DiscussionUpdate(title='mod'), admin_user, db)
    assert updated.title == 'mod'
    comment = await comments_service.update_comment(_req(), 'comment_73', DiscussionCommentUpdate(content='mod'), admin_user, db)
    assert comment.content == 'mod'

async def test_removed_member_cannot_edit(db, thread):
    poster, discussion = thread
    await _remove_from_org(db, poster.id)
    with pytest.raises(HTTPException) as exc:
        await discussions_service.update_discussion(_req(), discussion.discussion_uuid, DiscussionUpdate(title='new'), poster, db)
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as exc:
        await comments_service.update_comment(_req(), 'comment_72', DiscussionCommentUpdate(content='edited'), poster, db)
    assert exc.value.status_code == 403

async def test_comment_edit_on_orphan_comment_is_404(db, org, regular_user):
    db.add(DiscussionComment(id=90, discussion_id=999, author_id=regular_user.id, content='x', comment_uuid='comment_orphan', creation_date=NOW, update_date=NOW))
    await db.commit()
    with pytest.raises(HTTPException) as exc:
        await comments_service.update_comment(_req(), 'comment_orphan', DiscussionCommentUpdate(content='y'), regular_user, db)
    assert exc.value.status_code == 404

@pytest.mark.parametrize('params', [{'discussion_uuid': 'discussion_thread'}, {'comment_uuid': 'comment_72'}, {'course_uuid': 'course_test'}], ids=['discussion', 'comment', 'course'])
async def test_plan_gate_resolves_org_from_child(db, org, thread, course, params):
    plan = AsyncMock(side_effect=_DENY)
    with patch.object(discussions_router, 'check_org_plan', new=plan):
        with pytest.raises(HTTPException) as exc:
            await discussions_router.require_communities_plan_by_child(_req(params), db)
    assert exc.value.status_code == 403
    plan.assert_awaited_once_with(org.id, 'standard', 'Communities', db)

@pytest.mark.parametrize('params', [{}, {'discussion_uuid': 'discussion_missing'}], ids=['none', 'unknown'])
async def test_plan_gate_skips_without_child_org(db, params):
    plan = AsyncMock()
    with patch.object(discussions_router, 'check_org_plan', new=plan):
        await discussions_router.require_communities_plan_by_child(_req(params), db)
    plan.assert_not_awaited()

def test_plan_gate_is_mounted_on_child_routes():
    from src.routers.communities import communities as communities_router
    gate = discussions_router.require_communities_plan_by_child
    assert any((d.dependency is gate for d in discussions_router.router.dependencies))
    course_route = next((r for r in communities_router.router.routes if r.path == '/course/{course_uuid}'))
    assert any((d.dependency is gate for d in course_route.dependencies))

def _upload(name: str) -> UploadFile:
    return UploadFile(filename=name, file=BytesIO(b'x'))

async def _search(db, org, user):
    with patch.object(search_service, 'search_courses', new=AsyncMock(return_value=[])):
        return await search_service.search_across_org(_req(), user, org.slug, 'match', db, limit=50)

@pytest.fixture
async def searchable(db, org):
    for cid, public in ((110, True), (111, False)):
        db.add(Community(id=cid, org_id=org.id, name='match', description='', public=public, community_uuid=f'community_{cid}', creation_date=NOW, update_date=NOW))
    await db.commit()

async def test_search_token_without_buckets_sees_public_only(db, org, searchable, admin_user):
    result = await _search(db, org, _token(org, search=_READ))
    assert result.users == []
    assert {c.community_uuid for c in result.communities} == {'community_110'}

async def test_search_token_with_buckets(db, org, searchable, admin_user, regular_user):
    token = _token(org, search=_READ, users=_READ, communities=_READ)
    result = await _search(db, org, token)
    assert {u.username for u in result.users} == set()
    assert {c.community_uuid for c in result.communities} == {'community_110', 'community_111'}

async def test_search_token_users_bucket(db, org, admin_user, regular_user):
    with patch.object(search_service, 'search_courses', new=AsyncMock(return_value=[])):
        without = await search_service.search_across_org(_req(), _token(org, search=_READ), org.slug, 'regular', db)
        with_users = await search_service.search_across_org(_req(), _token(org, search=_READ, users=_READ), org.slug, 'regular', db)
    assert without.users == []
    assert [u.username for u in with_users.users] == ['regular']

@pytest.fixture
async def worded(db, org):
    community = Community(id=120, org_id=org.id, name='w', description='', public=True, community_uuid='community_worded', moderation_words=['secret'], creation_date=NOW, update_date=NOW)
    db.add(community)
    await db.commit()
    return community

async def test_get_community_words_only_for_moderators(db, org, worded, admin_user, regular_user, anonymous_user):
    as_admin = await communities_service.get_community(_req(), worded.community_uuid, admin_user, db)
    assert as_admin.moderation_words == ['secret']
    for reader in (regular_user, anonymous_user):
        read = await communities_service.get_community(_req(), worded.community_uuid, reader, db)
        assert read.moderation_words == []

async def test_get_community_policy_error_hides_words(db, worded, admin_user):
    access = AsyncMock(side_effect=[None, HTTPException(status_code=403)])
    with patch.object(communities_service, 'check_resource_access', new=access):
        read = await communities_service.get_community(_req(), worded.community_uuid, admin_user, db)
    assert read.moderation_words == []

async def test_community_listings_hide_words(db, org, course, worded, regular_user, anonymous_user):
    for reader in (regular_user, anonymous_user, _token(org, communities=_READ)):
        listed = await communities_service.get_communities_by_org(_req(), org.id, reader, db)
        assert [c.moderation_words for c in listed] == [[]]
    worded.course_id = course.id
    db.add(worded)
    await db.commit()
    by_course = await communities_service.get_community_by_course(_req(), course.course_uuid, regular_user, db)
    assert by_course.moderation_words == []
