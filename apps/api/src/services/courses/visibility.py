"""Batch visibility checks for course cards shown in learning paths."""
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
from src.db.courses.courses import Course
from src.db.resource_authors import ResourceAuthor, ResourceAuthorshipStatusEnum
from src.db.usergroup_resources import UserGroupResource
from src.security.org_auth import is_org_admin
from src.services.courses.locks import batch_accessible_restricted_uuids


async def hidden_course_uuids(db: AsyncSession, courses: list[Course], include_private: bool, viewer_id: int) -> set[str]:
    if not include_private:
        return {c.course_uuid for c in courses if not (c.public and c.published)}
    if not courses:
        return set()
    org_ids = {c.org_id for c in courses}
    if len(org_ids) == 1 and viewer_id and await is_org_admin(viewer_id, next(iter(org_ids)), db):
        return set()
    uuids = [c.course_uuid for c in courses]
    linked = set((await db.execute(select(UserGroupResource.resource_uuid).where(
        UserGroupResource.resource_uuid.in_(uuids)
    ))).scalars().all())
    granted = set()
    if viewer_id:
        granted = await batch_accessible_restricted_uuids(viewer_id, uuids, db)
        granted |= set((await db.execute(select(ResourceAuthor.resource_uuid).where(
            ResourceAuthor.resource_uuid.in_(uuids), ResourceAuthor.user_id == viewer_id,
            ResourceAuthor.authorship_status == ResourceAuthorshipStatusEnum.ACTIVE,
        ))).scalars().all())
    return {c.course_uuid for c in courses if c.course_uuid not in granted
            and not (c.published and (c.public or c.course_uuid not in linked))}
