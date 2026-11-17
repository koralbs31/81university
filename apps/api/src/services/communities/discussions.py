from typing import List, Union, Optional
from uuid import uuid4
from datetime import datetime, timezone
from enum import Enum
from sqlalchemy import func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
from fastapi import HTTPException, Request
from src.db.users import PublicUser, AnonymousUser, APITokenUser, User, UserReadAuthor
from src.security.auth import resolve_acting_user_id
from src.db.communities.communities import Community
from src.services.analytics.analytics import track
from src.services.analytics import events as analytics_events
from src.services.webhooks.dispatch import dispatch_webhooks
from src.db.communities.discussions import Discussion, DiscussionReadWithCounts, DiscussionReadWithVoteStatus, DiscussionUpdate, DISCUSSION_LABELS
from src.db.communities.discussion_comments import DiscussionComment
from src.db.communities.discussion_votes import DiscussionVote
from src.security.rbac import check_resource_access, AccessAction, authorization_verify_if_user_is_anon, authorization_verify_based_on_org_admin_status
from src.services.communities.moderation import validate_discussion_content, enforce_posting_limits
from src.services.communities.access import require_author_may_still_post, require_community_participant, require_token_in_community_org
from src.security.api_token_utils import token_has_right

class DiscussionSortBy(str, Enum):
    RECENT = 'recent'
    UPVOTES = 'upvotes'
    HOT = 'hot'

def calculate_hot_score(upvotes: int, creation_date: str) -> float:
    """
    Calculate hot score using Hacker News algorithm.

    Formula: (votes - 1) / (hours + 2)^1.8

    This ensures:
    - New posts get initial visibility
    - Posts decay naturally over time
    - High-voted posts stay visible longer
    """
    GRAVITY = 1.8
    BASE_HOURS = 2
    try:
        created = datetime.fromisoformat(creation_date.replace('Z', '+00:00'))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        created = datetime.now(timezone.utc)
    now = datetime.now(timezone.utc)
    hours = max(0, (now - created).total_seconds() / 3600)
    return (upvotes - 1) / pow(hours + BASE_HOURS, GRAVITY)

def validate_label(label: str) -> str:
    """Validate that the label is a valid discussion label."""
    valid_labels = {label_def['id'] for label_def in DISCUSSION_LABELS}
    if label not in valid_labels:
        return 'general'
    return label

async def _can_moderate(request: Request, current_user: Union[PublicUser, AnonymousUser, APITokenUser], acting_user_id: int, action: AccessAction, community: Community, db_session: AsyncSession) -> bool:
    """Whether the caller may moderate discussions of ``community``.

    API tokens moderate through their own rights, never their creator's role.
    """
    if isinstance(current_user, APITokenUser):
        decision = await check_resource_access(request, db_session, current_user, community.community_uuid, action, raise_on_deny=False)
        return decision.allowed
    return await authorization_verify_based_on_org_admin_status(request, acting_user_id, action.value, community.community_uuid, db_session)

def _is_author(current_user: Union[PublicUser, AnonymousUser, APITokenUser], acting_user_id: int, discussion: Discussion) -> bool:
    """A token edits its creator's posts only while it may post itself."""
    if discussion.author_id != acting_user_id:
        return False
    if isinstance(current_user, APITokenUser):
        return token_has_right(current_user, 'discussions', 'action_create')
    return True

async def _comment_counts(db_session: AsyncSession, discussion_ids: List[int]) -> dict[int, int]:
    """Comment totals per discussion, in one grouped query.

    Counts exactly what ``get_comment_count`` counts: every comment row on the
    discussion. Discussions without comments are absent from the map.
    """
    if not discussion_ids:
        return {}
    statement = select(DiscussionComment.discussion_id, func.count(DiscussionComment.id)).where(DiscussionComment.discussion_id.in_(discussion_ids)).group_by(DiscussionComment.discussion_id)
    rows = (await db_session.execute(statement)).all()
    return {discussion_id: count for discussion_id, count in rows}

async def create_discussion(request: Request, community_uuid: str, title: str, content: str, label: str, current_user: Union[PublicUser, AnonymousUser, APITokenUser], db_session: AsyncSession, emoji: Optional[str]=None) -> DiscussionReadWithVoteStatus:
    """
    Create a new discussion in a community.

    Requires a member of the community's org allowed to post who can read
    the community. Author automatically upvotes their own discussion.
    """
    await authorization_verify_if_user_is_anon(current_user.id)
    await check_resource_access(request, db_session, current_user, community_uuid, AccessAction.READ)
    community_statement = select(Community).where(Community.community_uuid == community_uuid)
    community = (await db_session.execute(community_statement)).scalars().first()
    if not community:
        raise HTTPException(status_code=404, detail='Community not found')
    author_id = await require_community_participant(current_user, community, db_session)
    await enforce_posting_limits(author_id, community, db_session)
    await validate_discussion_content(title, content, community.id, db_session)
    validated_label = validate_label(label)
    discussion = Discussion(title=title, content=content, label=validated_label, emoji=emoji, community_id=community.id, org_id=community.org_id, author_id=author_id, discussion_uuid=f'discussion_{uuid4()}', upvote_count=1, is_pinned=False, is_locked=False, creation_date=str(datetime.now()), update_date=str(datetime.now()))
    db_session.add(discussion)
    await db_session.flush()
    vote = DiscussionVote(discussion_id=discussion.id, user_id=author_id, vote_uuid=f'vote_{uuid4()}', creation_date=str(datetime.now()))
    db_session.add(vote)
    await db_session.commit()
    await db_session.refresh(discussion)
    await track(event_name=analytics_events.DISCUSSION_POSTED, org_id=community.org_id, user_id=author_id, properties={'community_uuid': community_uuid, 'discussion_uuid': discussion.discussion_uuid, 'label': validated_label})
    author_statement = select(User).where(User.id == discussion.author_id)
    author = (await db_session.execute(author_statement)).scalars().first()
    await dispatch_webhooks(event_name=analytics_events.DISCUSSION_POSTED, org_id=community.org_id, data={'user': {'user_uuid': author.user_uuid if author else '', 'email': author.email if author else '', 'username': author.username if author else ''}, 'discussion': {'discussion_uuid': discussion.discussion_uuid, 'title': discussion.title}, 'community': {'community_uuid': community_uuid}})
    return DiscussionReadWithVoteStatus(**discussion.model_dump(), author=UserReadAuthor.model_validate(author.model_dump()) if author else None, has_voted=True)

async def get_discussion(request: Request, discussion_uuid: str, current_user: Union[PublicUser, AnonymousUser, APITokenUser], db_session: AsyncSession) -> DiscussionReadWithCounts:
    """
    Get a discussion by UUID.
    """
    statement = select(Discussion).where(Discussion.discussion_uuid == discussion_uuid)
    discussion = (await db_session.execute(statement)).scalars().first()
    if not discussion:
        raise HTTPException(status_code=404, detail='Discussion not found')
    community_statement = select(Community).where(Community.id == discussion.community_id)
    community = (await db_session.execute(community_statement)).scalars().first()
    if not community:
        raise HTTPException(status_code=404, detail='Community not found')
    await check_resource_access(request, db_session, current_user, community.community_uuid, AccessAction.READ)
    author_statement = select(User).where(User.id == discussion.author_id)
    author = (await db_session.execute(author_statement)).scalars().first()
    has_voted = False
    viewer_id = resolve_acting_user_id(current_user)
    if viewer_id != 0:
        vote_statement = select(DiscussionVote).where(DiscussionVote.discussion_id == discussion.id, DiscussionVote.user_id == viewer_id)
        vote = (await db_session.execute(vote_statement)).scalars().first()
        has_voted = vote is not None
    comment_counts = await _comment_counts(db_session, [discussion.id])
    return DiscussionReadWithCounts(**discussion.model_dump(), author=UserReadAuthor.model_validate(author.model_dump()) if author else None, has_voted=has_voted, comment_count=comment_counts.get(discussion.id, 0))

async def get_discussions_by_community(request: Request, community_uuid: str, current_user: Union[PublicUser, AnonymousUser, APITokenUser], db_session: AsyncSession, sort_by: DiscussionSortBy=DiscussionSortBy.RECENT, page: int=1, limit: int=10, label: Optional[str]=None) -> List[DiscussionReadWithCounts]:
    """
    Get paginated list of discussions for a community with sorting.
    Pinned discussions are always returned first.
    """
    await check_resource_access(request, db_session, current_user, community_uuid, AccessAction.READ)
    community_statement = select(Community).where(Community.community_uuid == community_uuid)
    community = (await db_session.execute(community_statement)).scalars().first()
    if not community:
        raise HTTPException(status_code=404, detail='Community not found')
    offset = (page - 1) * limit
    query = select(Discussion).where(Discussion.community_id == community.id)
    if label:
        query = query.where(Discussion.label == label)
    pinned_discussions = []
    if page == 1:
        pinned_query = select(Discussion).where(Discussion.community_id == community.id, Discussion.is_pinned == True)
        if label:
            pinned_query = pinned_query.where(Discussion.label == label)
        pinned_discussions = list((await db_session.execute(pinned_query)).scalars().all())
    non_pinned_query = select(Discussion).where(Discussion.community_id == community.id, Discussion.is_pinned == False)
    if label:
        non_pinned_query = non_pinned_query.where(Discussion.label == label)
    if sort_by == DiscussionSortBy.RECENT:
        non_pinned_query = non_pinned_query.order_by(Discussion.creation_date.desc())
    elif sort_by == DiscussionSortBy.UPVOTES:
        non_pinned_query = non_pinned_query.order_by(Discussion.upvote_count.desc())
    pinned_count = len(pinned_discussions) if page == 1 else 0
    adjusted_offset = max(0, offset - pinned_count) if page > 1 else 0
    adjusted_limit = limit - pinned_count if page == 1 else limit
    if adjusted_limit > 0:
        non_pinned_query = non_pinned_query.offset(adjusted_offset).limit(adjusted_limit)
        non_pinned_discussions = list((await db_session.execute(non_pinned_query)).scalars().all())
    else:
        non_pinned_discussions = []
    if sort_by == DiscussionSortBy.HOT and non_pinned_discussions:
        discussions_with_scores = [(d, calculate_hot_score(d.upvote_count, d.creation_date)) for d in non_pinned_discussions]
        discussions_with_scores.sort(key=lambda x: x[1], reverse=True)
        non_pinned_discussions = [d[0] for d in discussions_with_scores]
    discussions = (pinned_discussions if page == 1 else []) + non_pinned_discussions
    discussion_ids = [d.id for d in discussions]
    author_ids = [d.author_id for d in discussions]
    if author_ids:
        authors_query = select(User).where(User.id.in_(author_ids))
        authors = (await db_session.execute(authors_query)).scalars().all()
        authors_map = {a.id: a for a in authors}
    else:
        authors_map = {}
    user_votes = set()
    viewer_id = resolve_acting_user_id(current_user)
    if viewer_id != 0 and discussion_ids:
        votes_query = select(DiscussionVote).where(DiscussionVote.discussion_id.in_(discussion_ids), DiscussionVote.user_id == viewer_id)
        votes = (await db_session.execute(votes_query)).scalars().all()
        user_votes = {v.discussion_id for v in votes}
    comment_counts = await _comment_counts(db_session, discussion_ids)
    result = []
    for discussion in discussions:
        author = authors_map.get(discussion.author_id)
        has_voted = discussion.id in user_votes
        result.append(DiscussionReadWithCounts(**discussion.model_dump(), author=UserReadAuthor.model_validate(author.model_dump()) if author else None, has_voted=has_voted, comment_count=comment_counts.get(discussion.id, 0)))
    return result

async def update_discussion(request: Request, discussion_uuid: str, discussion_object: DiscussionUpdate, current_user: Union[PublicUser, AnonymousUser, APITokenUser], db_session: AsyncSession) -> DiscussionReadWithVoteStatus:
    """
    Update a discussion.

    Requires discussion author or admin role.
    Authors can edit their discussions up to 2 times.
    """
    acting_user_id = resolve_acting_user_id(current_user)
    await authorization_verify_if_user_is_anon(acting_user_id)
    statement = select(Discussion).where(Discussion.discussion_uuid == discussion_uuid)
    discussion = (await db_session.execute(statement)).scalars().first()
    if not discussion:
        raise HTTPException(status_code=404, detail='Discussion not found')
    community_statement = select(Community).where(Community.id == discussion.community_id)
    community = (await db_session.execute(community_statement)).scalars().first()
    if not community:
        raise HTTPException(status_code=404, detail='Community not found')
    require_token_in_community_org(current_user, community)
    is_author = _is_author(current_user, acting_user_id, discussion)
    is_admin = await _can_moderate(request, current_user, acting_user_id, AccessAction.UPDATE, community, db_session)
    if not is_author and (not is_admin):
        raise HTTPException(status_code=403, detail="You don't have permission to update this discussion")
    if not is_admin:
        await require_author_may_still_post(request, current_user, community, discussion, db_session)
    max_edits = 2
    if is_author and discussion.edit_count >= max_edits:
        raise HTTPException(status_code=403, detail={'code': 'EDIT_LIMIT_REACHED', 'message': f'You have reached the maximum number of edits ({max_edits}). You can no longer edit this discussion.', 'edit_count': discussion.edit_count, 'max_edits': max_edits})
    title_to_check = discussion_object.title if discussion_object.title is not None else discussion.title
    content_to_check = discussion_object.content if discussion_object.content is not None else discussion.content
    await validate_discussion_content(title_to_check, content_to_check or '', discussion.community_id, db_session)
    if discussion_object.title is not None:
        discussion.title = discussion_object.title
    if discussion_object.content is not None:
        discussion.content = discussion_object.content
    if discussion_object.label is not None:
        discussion.label = validate_label(discussion_object.label)
    if discussion_object.emoji is not None:
        discussion.emoji = discussion_object.emoji if discussion_object.emoji else None
    if is_author:
        discussion.edit_count += 1
    discussion.update_date = str(datetime.now())
    db_session.add(discussion)
    await db_session.commit()
    await db_session.refresh(discussion)
    author_statement = select(User).where(User.id == discussion.author_id)
    author = (await db_session.execute(author_statement)).scalars().first()
    vote_statement = select(DiscussionVote).where(DiscussionVote.discussion_id == discussion.id, DiscussionVote.user_id == acting_user_id)
    vote = (await db_session.execute(vote_statement)).scalars().first()
    return DiscussionReadWithVoteStatus(**discussion.model_dump(), author=UserReadAuthor.model_validate(author.model_dump()) if author else None, has_voted=vote is not None)

async def pin_discussion(request: Request, discussion_uuid: str, is_pinned: bool, current_user: Union[PublicUser, AnonymousUser, APITokenUser], db_session: AsyncSession) -> DiscussionReadWithVoteStatus:
    """
    Pin or unpin a discussion.

    Requires community admin or maintainer role. Authors cannot pin.
    """
    acting_user_id = resolve_acting_user_id(current_user)
    await authorization_verify_if_user_is_anon(acting_user_id)
    statement = select(Discussion).where(Discussion.discussion_uuid == discussion_uuid)
    discussion = (await db_session.execute(statement)).scalars().first()
    if not discussion:
        raise HTTPException(status_code=404, detail='Discussion not found')
    community_statement = select(Community).where(Community.id == discussion.community_id)
    community = (await db_session.execute(community_statement)).scalars().first()
    if not community:
        raise HTTPException(status_code=404, detail='Community not found')
    require_token_in_community_org(current_user, community)
    is_admin = await _can_moderate(request, current_user, acting_user_id, AccessAction.UPDATE, community, db_session)
    if not is_admin:
        raise HTTPException(status_code=403, detail='Only community moderators can pin discussions')
    discussion.is_pinned = is_pinned
    discussion.update_date = str(datetime.now())
    db_session.add(discussion)
    await db_session.commit()
    await db_session.refresh(discussion)
    await dispatch_webhooks(event_name='discussion_pinned', org_id=community.org_id, data={'discussion_uuid': discussion.discussion_uuid, 'title': discussion.title, 'is_pinned': is_pinned, 'community_uuid': community.community_uuid})
    author_statement = select(User).where(User.id == discussion.author_id)
    author = (await db_session.execute(author_statement)).scalars().first()
    vote_statement = select(DiscussionVote).where(DiscussionVote.discussion_id == discussion.id, DiscussionVote.user_id == acting_user_id)
    vote = (await db_session.execute(vote_statement)).scalars().first()
    return DiscussionReadWithVoteStatus(**discussion.model_dump(), author=UserReadAuthor.model_validate(author.model_dump()) if author else None, has_voted=vote is not None)

async def lock_discussion(request: Request, discussion_uuid: str, is_locked: bool, current_user: Union[PublicUser, AnonymousUser, APITokenUser], db_session: AsyncSession) -> DiscussionReadWithVoteStatus:
    """
    Lock or unlock a discussion.

    Requires community admin or maintainer role. Authors cannot lock.
    """
    acting_user_id = resolve_acting_user_id(current_user)
    await authorization_verify_if_user_is_anon(acting_user_id)
    statement = select(Discussion).where(Discussion.discussion_uuid == discussion_uuid)
    discussion = (await db_session.execute(statement)).scalars().first()
    if not discussion:
        raise HTTPException(status_code=404, detail='Discussion not found')
    community_statement = select(Community).where(Community.id == discussion.community_id)
    community = (await db_session.execute(community_statement)).scalars().first()
    if not community:
        raise HTTPException(status_code=404, detail='Community not found')
    require_token_in_community_org(current_user, community)
    is_admin = await _can_moderate(request, current_user, acting_user_id, AccessAction.UPDATE, community, db_session)
    if not is_admin:
        raise HTTPException(status_code=403, detail='Only community moderators can lock discussions')
    discussion.is_locked = is_locked
    discussion.update_date = str(datetime.now())
    db_session.add(discussion)
    await db_session.commit()
    await db_session.refresh(discussion)
    await dispatch_webhooks(event_name='discussion_locked', org_id=community.org_id, data={'discussion_uuid': discussion.discussion_uuid, 'title': discussion.title, 'is_locked': is_locked, 'community_uuid': community.community_uuid})
    author_statement = select(User).where(User.id == discussion.author_id)
    author = (await db_session.execute(author_statement)).scalars().first()
    vote_statement = select(DiscussionVote).where(DiscussionVote.discussion_id == discussion.id, DiscussionVote.user_id == acting_user_id)
    vote = (await db_session.execute(vote_statement)).scalars().first()
    return DiscussionReadWithVoteStatus(**discussion.model_dump(), author=UserReadAuthor.model_validate(author.model_dump()) if author else None, has_voted=vote is not None)

async def delete_discussion(request: Request, discussion_uuid: str, current_user: Union[PublicUser, AnonymousUser, APITokenUser], db_session: AsyncSession) -> dict:
    """
    Delete a discussion.

    Requires discussion author or admin role.
    """
    acting_user_id = resolve_acting_user_id(current_user)
    await authorization_verify_if_user_is_anon(acting_user_id)
    statement = select(Discussion).where(Discussion.discussion_uuid == discussion_uuid)
    discussion = (await db_session.execute(statement)).scalars().first()
    if not discussion:
        raise HTTPException(status_code=404, detail='Discussion not found')
    community_statement = select(Community).where(Community.id == discussion.community_id)
    community = (await db_session.execute(community_statement)).scalars().first()
    if not community:
        raise HTTPException(status_code=404, detail='Community not found')
    require_token_in_community_org(current_user, community)
    is_author = _is_author(current_user, acting_user_id, discussion)
    is_admin = await _can_moderate(request, current_user, acting_user_id, AccessAction.DELETE, community, db_session)
    if not is_author and (not is_admin):
        raise HTTPException(status_code=403, detail="You don't have permission to delete this discussion")
    await db_session.delete(discussion)
    await db_session.commit()
    return {'detail': 'Discussion deleted'}
