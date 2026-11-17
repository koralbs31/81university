from typing import Literal, Optional
import json
import logging
from src.db.courses.courses import Course
from src.db.organizations import Organization
from pydantic import BaseModel
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
from src.db.courses.chapters import Chapter
from src.db.courses.activities import Activity, ActivityRead, ActivitySubTypeEnum, ActivityTypeEnum
from src.db.courses.chapter_activities import ChapterActivity
from src.db.courses.course_chapters import CourseChapter
from src.db.users import AnonymousUser, PublicUser
from src.services.courses.activities.uploads.videos import upload_video
from fastapi import HTTPException, status, UploadFile, Request
from uuid import uuid4
from datetime import datetime
from src.security.rbac import check_resource_access, AccessAction
from src.services.courses.activities.activities import keep_server_owned_metadata
from src.services.courses.activities.utils import track_activity_created
logger = logging.getLogger(__name__)

async def create_video_activity(request: Request, name: str, chapter_id: int, current_user: PublicUser, db_session: AsyncSession, video_file: UploadFile | None=None, details: str='{}', extra_metadata: Optional[dict]=None):
    statement = select(Chapter).where(Chapter.id == chapter_id)
    chapter = (await db_session.execute(statement)).scalars().first()
    details = json.loads(details)
    if not chapter:
        raise HTTPException(status_code=404, detail='Chapter not found')
    statement = select(CourseChapter).where(CourseChapter.chapter_id == chapter_id)
    coursechapter = (await db_session.execute(statement)).scalars().first()
    if not coursechapter:
        raise HTTPException(status_code=404, detail='CourseChapter not found')
    statement = select(Course).where(Course.id == coursechapter.course_id)
    course = (await db_session.execute(statement)).scalars().first()
    if not course:
        raise HTTPException(status_code=404, detail='Course not found')
    await check_resource_access(request, db_session, current_user, course.course_uuid, AccessAction.CREATE)
    statement = select(Organization).where(Organization.id == coursechapter.org_id)
    organization = (await db_session.execute(statement)).scalars().first()
    activity_uuid = str(f'activity_{uuid4()}')
    if not video_file:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='Video : No video file provided')
    if video_file.content_type not in ['video/mp4', 'video/webm']:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='Video : Wrong video format')
    if not video_file.filename:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='Video : No video file provided')
    saved_filename = None
    if video_file and organization and course:
        saved_filename = await upload_video(video_file, activity_uuid, organization.org_uuid, course.course_uuid)
    activity_object = Activity(name=name, activity_type=ActivityTypeEnum.TYPE_VIDEO, activity_sub_type=ActivitySubTypeEnum.SUBTYPE_VIDEO_HOSTED, activity_uuid=activity_uuid, org_id=coursechapter.org_id, course_id=coursechapter.course_id, content={'filename': saved_filename or 'video', 'activity_uuid': activity_uuid}, details=details if isinstance(details, dict) else json.loads(details), creation_date=str(datetime.now()), update_date=str(datetime.now()), extra_metadata=extra_metadata and keep_server_owned_metadata(extra_metadata, None))
    activity = Activity.model_validate(activity_object)
    db_session.add(activity)
    await db_session.commit()
    await db_session.refresh(activity)
    statement = select(ChapterActivity).where(ChapterActivity.chapter_id == chapter.id).order_by(ChapterActivity.order)
    chapter_activities = (await db_session.execute(statement)).scalars().all()
    last_order = chapter_activities[-1].order if chapter_activities else 0
    to_be_used_order = last_order + 1
    chapter_activity_object = ChapterActivity(chapter_id=chapter.id, activity_id=activity.id, course_id=coursechapter.course_id, org_id=coursechapter.org_id, creation_date=str(datetime.now()), update_date=str(datetime.now()), order=to_be_used_order)
    db_session.add(chapter_activity_object)
    await db_session.commit()
    await db_session.refresh(chapter_activity_object)
    await track_activity_created(activity, course, current_user)
    try:
        from src.services.utils.hls_jobs import enqueue as enqueue_hls
        enqueue_hls(activity.activity_uuid)
    except Exception:
        logger.exception('Failed to enqueue HLS transcode for %s', activity.activity_uuid)
    return ActivityRead.model_validate(activity)

class ExternalVideo(BaseModel):
    name: str
    uri: str
    type: Literal['youtube', 'vimeo']
    chapter_id: int
    details: str = '{}'
    extra_metadata: Optional[dict] = None

class ExternalVideoInDB(BaseModel):
    activity_id: int

async def create_external_video_activity(request: Request, current_user: PublicUser | AnonymousUser, data: ExternalVideo, db_session: AsyncSession):
    statement = select(Chapter).where(Chapter.id == data.chapter_id)
    chapter = (await db_session.execute(statement)).scalars().first()
    if not chapter:
        raise HTTPException(status_code=404, detail='Chapter not found')
    statement = select(CourseChapter).where(CourseChapter.chapter_id == data.chapter_id)
    coursechapter = (await db_session.execute(statement)).scalars().first()
    if not coursechapter:
        raise HTTPException(status_code=404, detail='CourseChapter not found')
    statement = select(Course).where(Course.id == coursechapter.course_id)
    course = (await db_session.execute(statement)).scalars().first()
    if not course:
        raise HTTPException(status_code=404, detail='Course not found')
    await check_resource_access(request, db_session, current_user, course.course_uuid, AccessAction.CREATE)
    activity_uuid = str(f'activity_{uuid4()}')
    details = json.loads(data.details)
    activity_object = Activity(name=data.name, activity_type=ActivityTypeEnum.TYPE_VIDEO, activity_sub_type=ActivitySubTypeEnum.SUBTYPE_VIDEO_YOUTUBE, activity_uuid=activity_uuid, course_id=coursechapter.course_id, org_id=coursechapter.org_id, content={'uri': data.uri, 'type': data.type, 'activity_uuid': activity_uuid}, details=details, creation_date=str(datetime.now()), update_date=str(datetime.now()), extra_metadata=data.extra_metadata and keep_server_owned_metadata(data.extra_metadata, None))
    activity = Activity.model_validate(activity_object)
    db_session.add(activity)
    await db_session.commit()
    await db_session.refresh(activity)
    statement = select(ChapterActivity).where(ChapterActivity.chapter_id == coursechapter.chapter_id).order_by(ChapterActivity.order)
    chapter_activities = (await db_session.execute(statement)).scalars().all()
    last_order = chapter_activities[-1].order if chapter_activities else 0
    to_be_used_order = last_order + 1
    chapter_activity_object = ChapterActivity(chapter_id=coursechapter.chapter_id, activity_id=activity.id, course_id=coursechapter.course_id, org_id=coursechapter.org_id, creation_date=str(datetime.now()), update_date=str(datetime.now()), order=to_be_used_order)
    db_session.add(chapter_activity_object)
    await db_session.commit()
    await track_activity_created(activity, course, current_user)
    return ActivityRead.model_validate(activity)

async def update_video_activity(request: Request, activity_uuid: str, current_user: PublicUser | AnonymousUser, db_session: AsyncSession, name: Optional[str]=None, video_file: UploadFile | None=None, details: Optional[str]=None) -> ActivityRead:
    statement = select(Activity).where(Activity.activity_uuid == activity_uuid)
    activity = (await db_session.execute(statement)).scalars().first()
    if not activity:
        raise HTTPException(status_code=404, detail='Activity not found')
    statement = select(Course).where(Course.id == activity.course_id)
    course = (await db_session.execute(statement)).scalars().first()
    if not course:
        raise HTTPException(status_code=404, detail='Course not found')
    await check_resource_access(request, db_session, current_user, course.course_uuid, AccessAction.UPDATE)
    if name is not None:
        activity.name = name
    if details:
        activity.details = json.loads(details)
    if video_file and video_file.filename:
        if video_file.content_type not in ['video/mp4', 'video/webm']:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='Video : Wrong video format')
        statement = select(Organization).where(Organization.id == activity.org_id)
        organization = (await db_session.execute(statement)).scalars().first()
        if organization and course:
            saved_filename = await upload_video(video_file, activity_uuid, organization.org_uuid, course.course_uuid)
            new_content = dict(activity.content) if activity.content else {}
            new_content['filename'] = saved_filename
            activity.content = new_content
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(activity, 'content')
    activity.update_date = str(datetime.now())
    db_session.add(activity)
    await db_session.commit()
    await db_session.refresh(activity)
    return ActivityRead.model_validate(activity)
class ChapterIn(BaseModel):
    start: float
    title: str

class ChaptersIn(BaseModel):
    items: list[ChapterIn] = []

async def _load_video_for_update(request: Request, activity_uuid: str, current_user: PublicUser | AnonymousUser, db_session: AsyncSession) -> Activity:
    """The hosted-video activity, after checking UPDATE rights on its course."""
    activity = (await db_session.execute(select(Activity).where(Activity.activity_uuid == activity_uuid))).scalars().first()
    if not activity:
        raise HTTPException(status_code=404, detail='Activity not found')
    if activity.activity_sub_type != ActivitySubTypeEnum.SUBTYPE_VIDEO_HOSTED:
        raise HTTPException(status_code=400, detail='Chapters are only available for hosted videos')
    course = (await db_session.execute(select(Course).where(Course.id == activity.course_id))).scalars().first()
    if not course:
        raise HTTPException(status_code=404, detail='Course not found')
    await check_resource_access(request, db_session, current_user, course.course_uuid, AccessAction.UPDATE)
    return activity

async def _store_chapters(activity: Activity, db_session: AsyncSession, chapters: dict) -> dict:
    from sqlalchemy.orm.attributes import flag_modified
    meta = dict(activity.extra_metadata or {})
    current = dict(meta.get('chapters') or {})
    current.update(chapters)
    current['updated_at'] = str(datetime.now())
    meta['chapters'] = current
    activity.extra_metadata = meta
    flag_modified(activity, 'extra_metadata')
    db_session.add(activity)
    await db_session.commit()
    return current

async def save_chapters(request: Request, activity_uuid: str, current_user: PublicUser | AnonymousUser, db_session: AsyncSession, payload: ChaptersIn) -> dict:
    """Replace a hosted video's chapters with an instructor's edited list. An
    empty list clears them."""
    activity = await _load_video_for_update(request, activity_uuid, current_user, db_session)
    from src.services.courses.video_chapters import MIN_CHAPTERS, normalize_chapters
    items = normalize_chapters([c.model_dump() for c in payload.items], None)
    if payload.items and (not items):
        raise HTTPException(status_code=400, detail=f'Provide at least {MIN_CHAPTERS} chapters with titles, at least 15 seconds apart')
    return await _store_chapters(activity, db_session, {'status': 'ready' if items else 'idle', 'items': items, 'model': None, 'error': None})

async def update_external_video_activity(request: Request, activity_uuid: str, current_user: PublicUser | AnonymousUser, db_session: AsyncSession, uri: Optional[str]=None, name: Optional[str]=None, details: Optional[str]=None) -> ActivityRead:
    statement = select(Activity).where(Activity.activity_uuid == activity_uuid)
    activity = (await db_session.execute(statement)).scalars().first()
    if not activity:
        raise HTTPException(status_code=404, detail='Activity not found')
    statement = select(Course).where(Course.id == activity.course_id)
    course = (await db_session.execute(statement)).scalars().first()
    if not course:
        raise HTTPException(status_code=404, detail='Course not found')
    await check_resource_access(request, db_session, current_user, course.course_uuid, AccessAction.UPDATE)
    if name is not None:
        activity.name = name
    if uri:
        content = dict(activity.content) if activity.content else {}
        content['uri'] = uri
        activity.content = content
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(activity, 'content')
    if details:
        activity.details = json.loads(details)
    activity.update_date = str(datetime.now())
    db_session.add(activity)
    await db_session.commit()
    await db_session.refresh(activity)
    return ActivityRead.model_validate(activity)
