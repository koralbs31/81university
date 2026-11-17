"""
Local Content Files Router

Serves static content files from the local filesystem with access control.
Replaces the unauthenticated StaticFiles mount to enforce authorization
on private course/podcast content while allowing public content through.

SECURITY:
- Activity content (videos, PDFs, blocks) for non-public courses requires auth
- Course-level metadata (thumbnails) is always public (shown in listings)
- Org-level content (logos, branding) is always public
- Podcast episode content for non-public podcasts requires auth
"""
import os
from pathlib import Path
from urllib.parse import unquote
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
from src.core.events.database import get_db_session
from src.db.courses.courses import Course
from src.db.organizations import Organization
from src.db.users import AnonymousUser, PublicUser, APITokenUser
from src.routers.content_files import content_cache_control
from src.security.auth import get_current_user
from src.services.courses.activities.access import verify_activity_reader_access_by_uuid
from src.security.submission_file_access import enforce_solution_file_access, enforce_submission_file_access, is_solution_file, is_submission_file, split_content_path
router = APIRouter()
CONTENT_DIR = Path('content')

def _normalize_content_relpath(file_path: str) -> str | None:
    """Decode and vet the request path, returning a safe CONTENT_DIR-relative
    string, or None if it is unsafe.

    Pure string handling only, no filesystem access. Each handler does the
    realpath + containment check inline against this value (see
    ``serve_local_content``); keeping the resolve-and-guard together with the
    filesystem sink, from the tainted input, is what makes the guard hold at
    runtime and what static analysis can follow.
    """
    decoded = unquote(unquote(file_path))
    if '..' in decoded or decoded.startswith('/') or '\x00' in decoded:
        return None
    normalized = decoded.replace('\\', '/')
    if '..' in normalized or normalized.startswith('/'):
        return None
    return normalized

async def _verify_course_in_org(org_uuid: str, course_uuid: str, db_session: AsyncSession) -> None:
    """404 unless ``course_uuid`` exists and belongs to the org ``org_uuid``."""
    course_org_uuid = (await db_session.execute(select(Organization.org_uuid).join(Course, Course.org_id == Organization.id).where(Course.course_uuid == course_uuid))).scalars().first()
    if course_org_uuid is None or course_org_uuid != org_uuid:
        raise HTTPException(status_code=404, detail='File not found')

async def _check_content_access(file_path: str, current_user: PublicUser | AnonymousUser | APITokenUser, db_session: AsyncSession, request: Request | None=None) -> bool:
    """
    Check if the user has access to the requested content.

    Returns True when the file is public by design (org branding, course
    thumbnails, avatars) and may be cached by shared caches; False for files
    served after a per-user check.

    Path patterns:
    - orgs/{uuid}/courses/{uuid}/activities/{uuid}/... → check course access
    - orgs/{uuid}/courses/{uuid}/thumbnails/...        → course thumbnail (public)
    - orgs/{uuid}/courses/{uuid}/{file}                → course metadata (public)
    - orgs/{uuid}/courses/{uuid}/{other}/...           → denied (e.g. SCORM)
    - orgs/{uuid}/podcasts/{uuid}/episodes/{uuid}/...  → check podcast access
    - orgs/{uuid}/...                                  → org-level (public)
    """
    parts = split_content_path(file_path)
    if len(parts) >= 6 and parts[0] == 'orgs' and (parts[2] == 'courses') and (parts[4] == 'activities'):
        await _verify_course_in_org(parts[1], parts[3], db_session)
    if is_submission_file(parts):
        await enforce_submission_file_access(parts, current_user, db_session, request)
        return False
    if len(parts) >= 6 and parts[0] == 'orgs' and (parts[2] == 'courses') and (parts[4] == 'activities'):
        course_uuid, activity_uuid = (parts[3], parts[5])
        await verify_activity_reader_access_by_uuid(request, activity_uuid, current_user, db_session, course_uuid=course_uuid)
        if is_solution_file(parts):
            await enforce_solution_file_access(parts, current_user, db_session, request)
        return False
    if len(parts) >= 3 and parts[0] == 'orgs' and (parts[2] == 'courses'):
        if len(parts) > 5 and parts[4] != 'thumbnails':
            raise HTTPException(status_code=403, detail='Access denied')
        return True
    if len(parts) >= 2 and parts[0] == 'orgs':
        return True
    if len(parts) >= 2 and parts[0] == 'users':
        return True
    if isinstance(current_user, AnonymousUser):
        raise HTTPException(status_code=401, detail='Authentication required')
    raise HTTPException(status_code=403, detail='Access denied')
_MIME_TYPES = {'.mp4': 'video/mp4', '.webm': 'video/webm', '.mp3': 'audio/mpeg', '.wav': 'audio/wav', '.ogg': 'audio/ogg', '.m4a': 'audio/mp4', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.gif': 'image/gif', '.webp': 'image/webp', '.ico': 'image/x-icon', '.pdf': 'application/pdf', '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation', '.zip': 'application/zip', '.json': 'application/json', '.txt': 'text/plain'}

@router.get('/content/{file_path:path}', summary='Serve a local content file', description='Streams a content file from the local filesystem with access control. Public course and podcast content is accessible anonymously; private content requires authentication and org membership.', responses={200: {'description': 'File served successfully'}, 400: {'description': 'Invalid or unsafe file path'}, 401: {'description': 'Authentication required to access this file'}, 403: {'description': 'User is not permitted to access this file'}, 404: {'description': 'File not found on disk'}})
async def serve_local_content(request: Request, file_path: str, current_user: PublicUser | AnonymousUser | APITokenUser=Depends(get_current_user), db_session: AsyncSession=Depends(get_db_session)):
    """
    Serve content files from local filesystem with access control.

    SECURITY: Validates user access based on resource ownership.
    Public courses/podcasts are accessible to anonymous users.
    Private content requires authentication.
    """
    rel_path = _normalize_content_relpath(file_path)
    if rel_path is None:
        raise HTTPException(status_code=400, detail='Invalid path')
    base_real = os.path.realpath(str(CONTENT_DIR))
    safe_real = os.path.realpath(os.path.join(base_real, rel_path))
    if not (safe_real == base_real or safe_real.startswith(base_real + os.sep)):
        raise HTTPException(status_code=400, detail='Invalid path')
    canonical_rel = os.path.relpath(safe_real, base_real).replace(os.sep, '/')
    is_public = await _check_content_access(canonical_rel, current_user, db_session, request=request)
    if not os.path.isfile(safe_real):
        raise HTTPException(status_code=404, detail='File not found')
    ext = os.path.splitext(safe_real)[1].lower()
    media_type = _MIME_TYPES.get(ext, 'application/octet-stream')
    return FileResponse(path=safe_real, media_type=media_type, headers={'Cache-Control': content_cache_control(is_public), 'X-Content-Type-Options': 'nosniff'})

@router.head('/content/{file_path:path}', summary='Get local content file metadata', description='Returns metadata for a local content file without the body. Used by clients to probe file size and MIME type before issuing a GET.', responses={200: {'description': 'File metadata returned via response headers'}, 400: {'description': 'Invalid or unsafe file path'}, 401: {'description': 'Authentication required to access this file'}, 403: {'description': 'User is not permitted to access this file'}, 404: {'description': 'File not found on disk'}})
async def head_local_content(request: Request, file_path: str, current_user: PublicUser | AnonymousUser | APITokenUser=Depends(get_current_user), db_session: AsyncSession=Depends(get_db_session)):
    """HEAD request for content files; returns metadata without body."""
    rel_path = _normalize_content_relpath(file_path)
    if rel_path is None:
        raise HTTPException(status_code=400, detail='Invalid path')
    base_real = os.path.realpath(str(CONTENT_DIR))
    safe_real = os.path.realpath(os.path.join(base_real, rel_path))
    if not (safe_real == base_real or safe_real.startswith(base_real + os.sep)):
        raise HTTPException(status_code=400, detail='Invalid path')
    canonical_rel = os.path.relpath(safe_real, base_real).replace(os.sep, '/')
    is_public = await _check_content_access(canonical_rel, current_user, db_session, request=request)
    if not os.path.isfile(safe_real):
        raise HTTPException(status_code=404, detail='File not found')
    ext = os.path.splitext(safe_real)[1].lower()
    media_type = _MIME_TYPES.get(ext, 'application/octet-stream')
    file_size = os.path.getsize(safe_real)
    from fastapi.responses import Response
    return Response(status_code=200, headers={'Accept-Ranges': 'bytes', 'Content-Length': str(file_size), 'Content-Type': media_type, 'Cache-Control': content_cache_control(is_public), 'X-Content-Type-Options': 'nosniff'})
