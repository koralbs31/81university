"""Loading and validating the demo content bundle.

The bundle is static data checked into the repository: one manifest, one file
per course, one assignment sidecar per course, and a small media directory.
Everything the demo organization contains is derived from it.

Two things are load-bearing and are enforced here rather than discovered at
runtime:

* **Every `bundle_key` is a stable identity.** The refresh matches database
  rows to bundle entries by key, so a renamed key is not an edit: it orphans
  the old row and creates a new one. Keys are validated for shape and
  uniqueness so a typo fails at load rather than silently duplicating content.
* **The bundle is fully validated before the first row is written.** Provision
  creates an organization, forty users and hundreds of content rows; finding a
  malformed activity halfway through would leave a half-built demo live.
"""
import json
import os
import re
from functools import lru_cache
from typing import Any, Optional
from pydantic import BaseModel, Field, field_validator
from src.services.demo.content import ContentError, compile_document, plain_text
BUNDLE_ROOT = os.path.join(os.path.dirname(__file__), 'bundle')
_KEY_RE = re.compile('^[a-z0-9][a-z0-9-]{1,62}$')
MAX_BUNDLE_BYTES = 8 * 1024 * 1024

class BundleError(ValueError):
    """The bundle on disk is malformed. Always an authoring bug, never input."""

def _validate_key(value: str) -> str:
    if not _KEY_RE.match(value):
        raise ValueError(f'{value!r} is not a valid bundle key (lowercase letters, digits, dashes)')
    return value

class ActivitySpec(BaseModel):
    slug: str
    name: str
    kind: str = 'document'
    published: bool = True
    body: list[Any] = Field(default_factory=list)
    youtube_id: Optional[str] = None
    _validate_slug = field_validator('slug')(_validate_key)

    @field_validator('kind')
    @classmethod
    def _known_kind(cls, value: str) -> str:
        if value not in ('document', 'video', 'assignment'):
            raise ValueError(f'unknown activity kind {value!r}')
        return value

    def compiled_content(self) -> dict:
        if self.kind == 'video':
            return {'type': 'doc', 'content': []}
        return compile_document(self.body)

    def word_count(self) -> int:
        return len(plain_text(self.body).split())

class ChapterSpec(BaseModel):
    slug: str
    name: str
    description: str = ''
    activities: list[ActivitySpec]
    _validate_slug = field_validator('slug')(_validate_key)

class CourseSpec(BaseModel):
    slug: str
    name: str
    description: str
    about: str = ''
    learnings: str = ''
    tags: str = ''
    thumbnail: str
    section: str
    public: bool = True
    published: bool = True
    certification: bool = False
    certification_name: Optional[str] = None
    certification_description: Optional[str] = None
    certification_type: str = 'completion'
    certificate_pattern: str = 'professional'
    chapters: list[ChapterSpec]
    _validate_slug = field_validator('slug')(_validate_key)

class PersonaSpec(BaseModel):
    id: str
    weight: int
    min_courses: int
    max_courses: int
    min_completion: float
    max_completion: float
    assignments: str = 'none'
    min_grade_pct: int = 0
    max_grade_pct: int = 0
    run_status: Optional[str] = None

class CohortSpec(BaseModel):
    slug: str
    name: str
    description: str = ''
    share: float
    restricted_courses: list[str] = Field(default_factory=list)
    _validate_slug = field_validator('slug')(_validate_key)

class SectionSpec(BaseModel):
    slug: str
    name: str
    color: str = 'violet'
    _validate_slug = field_validator('slug')(_validate_key)

class OrgSpec(BaseModel):
    name: str
    description: str
    about: str = ''
    email: str
    logo: str
    thumbnail: str

class StudentsSpec(BaseModel):
    count: int
    with_avatar: int
    avatar_count: int

class Manifest(BaseModel):
    bundle_version: str
    org: OrgSpec
    students: StudentsSpec
    sections: list[SectionSpec]
    cohorts: list[CohortSpec]
    personas: list[PersonaSpec]
    courses: list[str]

class CommentSpec(BaseModel):
    author: str
    content: str
    days_ago: float = 1
    upvotes: int = 0

class DiscussionSpec(BaseModel):
    slug: str
    title: str
    content: str
    label: str = 'general'
    emoji: Optional[str] = None
    author: str
    days_ago: float = 1
    upvotes: int = 0
    pinned: bool = False
    comments: list[CommentSpec] = Field(default_factory=list)
    _validate_slug = field_validator('slug')(_validate_key)

class CommunitySpec(BaseModel):
    slug: str
    name: str
    description: str = ''
    public: bool = True
    discussions: list[DiscussionSpec] = Field(default_factory=list)
    _validate_slug = field_validator('slug')(_validate_key)





class Engagement(BaseModel):
    """Boards, playgrounds and the community.

    Separate from courses because these are what make the *platform* look
    used rather than what makes a course look complete: an enabled feature
    with an empty page demonstrates nothing.
    """
    community: Optional[CommunitySpec] = None





class Bundle(BaseModel):
    manifest: Manifest
    courses: list[CourseSpec]
    assignments: dict[str, dict]
    engagement: Engagement = Field(default_factory=Engagement)

    def course_by_key(self, key: str) -> CourseSpec:
        for course in self.courses:
            if course.slug == key:
                return course
        raise BundleError(f'no course with key {key!r}')

    def media_path(self, *parts: str) -> str:
        return os.path.join(BUNDLE_ROOT, 'media', *parts)

def _read_json(path: str) -> dict:
    if not os.path.exists(path):
        raise BundleError(f'missing bundle file: {path}')
    with open(path, 'r', encoding='utf-8') as handle:
        try:
            return json.load(handle)
        except json.JSONDecodeError as exc:
            raise BundleError(f'{path} is not valid JSON: {exc}') from exc

def bundle_size_bytes(root: str=BUNDLE_ROOT) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            total += os.path.getsize(os.path.join(dirpath, name))
    return total

def _validate(bundle: Bundle) -> None:
    """Everything that must hold before a single row is written."""
    manifest = bundle.manifest
    section_keys = {section.slug for section in manifest.sections}
    if len(section_keys) != len(manifest.sections):
        raise BundleError('duplicate section keys in manifest')
    course_keys = [course.slug for course in bundle.courses]
    if len(set(course_keys)) != len(course_keys):
        raise BundleError('duplicate course keys')
    if set(course_keys) != set(manifest.courses):
        missing = set(manifest.courses) - set(course_keys)
        extra = set(course_keys) - set(manifest.courses)
        raise BundleError(f'manifest course list does not match course files (missing: {sorted(missing)}, unexpected: {sorted(extra)})')
    seen_chapters: set[str] = set()
    seen_activities: set[str] = set()
    for course in bundle.courses:
        if course.section not in section_keys:
            raise BundleError(f'course {course.slug!r} is in unknown section {course.section!r}')
        if not os.path.exists(bundle.media_path('courses', course.thumbnail)):
            raise BundleError(f'course {course.slug!r} references missing thumbnail {course.thumbnail!r}')
        if not course.chapters:
            raise BundleError(f'course {course.slug!r} has no chapters')
        for chapter in course.chapters:
            if chapter.slug in seen_chapters:
                raise BundleError(f'duplicate chapter key {chapter.slug!r}')
            seen_chapters.add(chapter.slug)
            if not chapter.activities:
                raise BundleError(f'chapter {chapter.slug!r} has no activities')
            for activity in chapter.activities:
                if activity.slug in seen_activities:
                    raise BundleError(f'duplicate activity key {activity.slug!r}')
                seen_activities.add(activity.slug)
                try:
                    activity.compiled_content()
                except ContentError as exc:
                    raise BundleError(f'activity {activity.slug!r} has invalid content: {exc}') from exc
                if activity.kind == 'video' and (not activity.youtube_id):
                    raise BundleError(f'video activity {activity.slug!r} has no youtube_id')
                if activity.kind == 'document' and activity.word_count() < 20:
                    raise BundleError(f'activity {activity.slug!r} has almost no content ({activity.word_count()} words)')
    for cohort in manifest.cohorts:
        for course_key in cohort.restricted_courses:
            if course_key not in course_keys:
                raise BundleError(f'cohort {cohort.slug!r} restricts unknown course {course_key!r}')
    total_weight = sum((persona.weight for persona in manifest.personas))
    if total_weight <= 0:
        raise BundleError('persona weights must sum to more than zero')
    students = manifest.students
    if students.with_avatar > students.count:
        raise BundleError('with_avatar exceeds the student count')
    assignment_activity_keys = {activity.slug for course in bundle.courses for chapter in course.chapters for activity in chapter.activities if activity.kind == 'assignment'}
    seen_task_keys: set[str] = set()
    for assignment_course_key, sidecar in bundle.assignments.items():
        if assignment_course_key not in course_keys:
            raise BundleError(f'assignment sidecar for unknown course {assignment_course_key!r}')
        activity_key = sidecar.get('activity_key')
        if activity_key not in assignment_activity_keys:
            raise BundleError(f'assignment sidecar {assignment_course_key!r} targets {activity_key!r}, which is not an assignment activity')
        tasks = sidecar.get('tasks') or []
        if not tasks:
            raise BundleError(f'assignment {assignment_course_key!r} has no tasks')
        for task in tasks:
            key = task.get('slug')
            if not key or key in seen_task_keys:
                raise BundleError(f'duplicate or missing task key {key!r}')
            seen_task_keys.add(key)
            templates = task.get('submissions') or {}
            missing = {'correct', 'partial', 'incorrect'} - set(templates)
            if missing:
                raise BundleError(f'task {key!r} is missing submission templates: {sorted(missing)}')
    covered = {sidecar.get('activity_key') for sidecar in bundle.assignments.values()}
    uncovered = assignment_activity_keys - covered
    if uncovered:
        raise BundleError(f'assignment activities with no sidecar: {sorted(uncovered)}')
    student_keys = {f'student-{i:02d}' for i in range(manifest.students.count)}
    engagement = bundle.engagement

    def _check_author(who: str, where: str) -> None:
        if who not in student_keys:
            raise BundleError(f'{where} is authored by unknown student {who!r}')
    if engagement.community:
        seen_discussions: set[str] = set()
        for discussion in engagement.community.discussions:
            if discussion.slug in seen_discussions:
                raise BundleError(f'duplicate discussion slug {discussion.slug!r}')
            seen_discussions.add(discussion.slug)
            _check_author(discussion.author, f'discussion {discussion.slug!r}')
            for comment in discussion.comments:
                _check_author(comment.author, f'a comment on {discussion.slug!r}')
    size = bundle_size_bytes()
    if size > MAX_BUNDLE_BYTES:
        raise BundleError(f'bundle is {size / 1048576:.1f} MB, over the {MAX_BUNDLE_BYTES / 1048576:.0f} MB budget')

def load_bundle(root: str=BUNDLE_ROOT) -> Bundle:
    """Read and fully validate the bundle. Raises BundleError if unusable."""
    manifest_data = _read_json(os.path.join(root, 'manifest.json'))
    try:
        manifest = Manifest.model_validate(manifest_data)
    except Exception as exc:
        raise BundleError(f'manifest.json is invalid: {exc}') from exc
    courses: list[CourseSpec] = []
    for key in manifest.courses:
        data = _read_json(os.path.join(root, 'courses', f'{key}.json'))
        try:
            courses.append(CourseSpec.model_validate(data))
        except Exception as exc:
            raise BundleError(f'courses/{key}.json is invalid: {exc}') from exc
    assignments: dict[str, dict] = {}
    assignments_dir = os.path.join(root, 'assignments')
    if os.path.isdir(assignments_dir):
        for name in sorted(os.listdir(assignments_dir)):
            if not name.endswith('.json'):
                continue
            assignments[name[:-5]] = _read_json(os.path.join(assignments_dir, name))
    engagement = Engagement()
    engagement_path = os.path.join(root, 'engagement.json')
    if os.path.exists(engagement_path):
        try:
            engagement = Engagement.model_validate(_read_json(engagement_path))
        except Exception as exc:
            raise BundleError(f'engagement.json is invalid: {exc}') from exc
    bundle = Bundle(manifest=manifest, courses=courses, assignments=assignments, engagement=engagement)
    _validate(bundle)
    return bundle

@lru_cache(maxsize=1)
def get_bundle() -> Bundle:
    """Process-wide cached bundle.

    The bundle is immutable data on disk, read on every refresh tick; parsing
    and validating it hourly for the life of the process would be pure waste.
    """
    return load_bundle()
