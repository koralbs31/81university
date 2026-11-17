"""Bring the demo organization into line with the bundle.

One function, ``sync_demo``, does both jobs the demo needs: it builds the
organization the first time, and it puts it back afterwards. There is
deliberately no separate "provision" and "refresh" implementation: two code
paths that must produce identical results are two code paths that eventually
do not, and the property this whole design rests on is that running the sync
twice in a row changes nothing.

That property is why the module is written the way it is:

* Rows are matched to bundle entries through the ``DemoEntity`` registry, so a
  second run finds and updates rather than duplicating.
* Fields are compared before assignment (``_apply``), so an unchanged row emits
  no UPDATE at all rather than an UPDATE that happens to write the same values.
* Timestamps are derived from ``content_epoch``, which rolls once a day, not
  from ``now``. Recomputing from the clock would change every backdated row on
  every tick.
* Media is uploaded only when the row does not already reference a file.

Anything inside the demo org without a registry entry was created by a visitor
and is deleted as drift.
"""
import logging
import secrets
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Any, NamedTuple, Optional
from uuid import UUID, uuid4, uuid5
from sqlalchemy import delete, func, select
from sqlmodel.ext.asyncio.session import AsyncSession
from src.db.api_tokens import APIToken
from src.db.communities.communities import Community
from src.db.communities.discussion_comment_votes import DiscussionCommentVote
from src.db.communities.discussion_comments import DiscussionComment
from src.db.communities.discussion_reactions import DiscussionReaction
from src.db.communities.discussion_votes import DiscussionVote
from src.db.communities.discussions import Discussion
from src.db.courses.activity_versions import ActivityVersion
from src.db.courses.activities import Activity, ActivitySubTypeEnum, ActivityTypeEnum
from src.db.courses.assignments import Assignment, AssignmentTask, AssignmentTaskSubmission, AssignmentUserSubmission
from src.db.courses.blocks import Block
from src.db.courses.certifications import CertificateUser, Certifications
from src.db.courses.chapter_activities import ChapterActivity
from src.db.courses.chapters import Chapter
from src.db.courses.course_chapters import CourseChapter
from src.db.courses.courses import Course, ThumbnailType
from src.db.demo_entities import DemoEntity, DemoEntityKind
from src.db.demo_state import DEMO_STATE_ID, DemoState, DemoStateEnum
from src.db.organization_config import OrganizationConfig
from src.db.organizations import Organization
from src.db.roles import Role
from src.db.resource_authors import ResourceAuthor, ResourceAuthorshipEnum, ResourceAuthorshipStatusEnum
from src.db.trail_runs import TrailRun
from src.db.trail_steps import TrailStep
from src.db.trails import Trail
from src.db.user_activity import UserActivityDay
from src.db.user_audit_events import UserAuditEvent
from src.db.user_organizations import UserOrganization
from src.db.webhooks import WebhookEndpoint
from src.db.usergroup_user import UserGroupUser
from src.db.usergroups import UserGroup
from src.db.users import User
from src.security.security import security_hash_password
from src.services.demo import flags
from src.services.demo.bundle_loader import Bundle, get_bundle
from src.services.demo.progress import HISTORY_DAYS, StudentPlan, activity_days, completed_activity_count, epoch_for, grade_for, plan_students, step_timestamp, submission_status_for, submission_template_for
from src.services.demo.registry import Registry
logger = logging.getLogger(__name__)
LEARNER_ROLE_ID = 4

class DemoSlugTakenError(RuntimeError):
    """The demo slug belongs to an organization that is not the demo."""

class _Sweep(NamedTuple):
    """One entry in the drift table: a model, its uuid column, its registry kind.

    Named rather than a bare 3-tuple so the loop reads as what it is. It also
    keeps CodeQL honest: unpacking anonymous tuples let its taint analysis
    conflate the elements, and it reported the sweep's `kind` (a constant like
    "course") as a password being written to the log.
    """
    model: Any
    uuid_column: Any
    kind: str

@dataclass
class SyncStats:
    created: int = 0
    updated: int = 0
    drift_deleted: int = 0
    steps: list[str] = field(default_factory=list)
    epoch: str = ''
    provisioned: bool = False

    def as_dict(self) -> dict:
        return {'created': self.created, 'updated': self.updated, 'drift_deleted': self.drift_deleted, 'epoch': self.epoch, 'provisioned': self.provisioned}

def _now() -> str:
    return str(datetime.now())

def _apply(instance: Any, **fields: Any) -> bool:
    """Assign only the fields that differ. Returns True if anything changed.

    The comparison is the point. Assigning an equal value still marks the
    attribute dirty in SQLAlchemy and emits an UPDATE on flush, which would
    turn every no-op refresh into several hundred pointless writes.
    """
    changed = False
    for name, value in fields.items():
        if getattr(instance, name) != value:
            setattr(instance, name, value)
            changed = True
    return changed

@lru_cache(maxsize=1)
def _unusable_password_hash() -> str:
    """A valid Argon2 hash of a secret that is generated and then discarded.

    Not an empty string: pwdlib raises UnknownHashError when it cannot identify
    a stored hash, and authenticate_user does not guard that call, so an empty
    password would turn a login attempt against a demo student into a 500
    rather than a clean 401.

    One hash is shared by all forty students. No plaintext for it exists
    anywhere, so sharing leaks nothing, and it saves forty Argon2 runs on every
    provision.
    """
    return security_hash_password(secrets.token_urlsafe(64))

def _prefixed(prefix: str) -> str:
    return f'{prefix}_{uuid4()}'

def _days_ago(epoch: str, days: float, hour: int=10) -> str:
    anchor = datetime.combine(date.fromisoformat(epoch), datetime.min.time())
    return str((anchor - timedelta(days=days)).replace(hour=hour))

def _demo_org_config() -> dict:
    """Config for the demo org: the pro plan, with the rest forced on.

    The demo exists to show the product, so it runs on `pro`, the tier that
    enables boards, playgrounds, communities, podcasts, collaboration,
    certifications, roles, versioning, webhooks, the API and analytics. Anything
    a prospect cannot see is a feature they will not buy.

    `overrides` then force on what even pro withholds (audit logs, SCORM, SSO
    are enterprise) and lift the seat and AI ceilings, because
    resolve_feature computes `available = plan_enabled or force_enabled` and
    adds `extra_limit` on top of the plan's number.

    **Running on a paid plan is safe only because the demo is excluded from the
    money paths, and those exclusions are load-bearing:**

    * `_enforce_free_org_cap` filters demo orgs out of the caller's admin orgs
      entirely, so a paid demo cannot mark a visitor "a customer, not
      free-tier" and permanently exempt them from the three-org cap on their
      own real organizations. That filter is what makes this line safe to
      change; without it, pro here would quietly hand every visitor unlimited
      free organizations.
    * `get_active_user_summary` returns a zeroed summary reporting plan
      "demo", which is not in the frontend's BILLABLE_PLANS: two independent
      barriers between synthetic activity and a Stripe invoice.
    * The billing routes and the plan-write endpoint refuse demo orgs, so a
      visitor cannot buy a subscription for a shared sandbox.

    Analytics is enabled because it is part of pro and a prospect should see
    the page exists. The seed writes no analytics events, so the charts only
    fill in with what visitors actually do in the demo. That is a known and
    deliberate limitation rather than an oversight.
    """
    return {'config_version': '2.0', 'active': True, 'plan': 'pro', 'admin_toggles': {'members': {'signup_mode': 'inviteOnly'}}, 'overrides': {'members': {'extra_limit': 500}, 'analytics_advanced': {'force_enabled': True}, 'custom_domains': {'force_enabled': True}, 'usergroups': {'force_enabled': True}, 'collaboration': {'force_enabled': True}, 'certifications': {'force_enabled': True}, 'versioning': {'force_enabled': True}, 'roles': {'force_enabled': True}, 'communities': {'force_enabled': True}, 'webhooks': {'force_enabled': True}, 'api': {'force_enabled': True}, 'seo': {'force_enabled': True}, 'analytics': {'force_enabled': True}}, 'customization': {}}

async def sync_demo(db_session: AsyncSession, *, today: Optional[date]=None) -> SyncStats:
    """Create or refresh the shared demo organization.

    Safe to call repeatedly. On a settled demo it reads a few hundred rows and
    writes none.

    A failure is recorded on the state row before being re-raised, so
    `demo-status` reports the truth. Without that the row keeps its last
    successful values and an operator looking at a demo that has been broken
    for hours sees state READY and no error.
    """
    started_from = await _state_token(db_session)
    try:
        return await _sync_demo_inner(db_session, today=today)
    except Exception as exc:
        await _record_sync_failure(db_session, exc, started_from=started_from)
        raise

async def _state_token(db_session: AsyncSession) -> Optional[str]:
    """The state row's last write, as a value that changes on every sync."""
    try:
        return (await db_session.execute(select(DemoState.update_date).where(DemoState.id == DEMO_STATE_ID))).scalars().first()
    except Exception:
        return None

async def _record_sync_failure(db_session: AsyncSession, exc: Exception, *, started_from: Optional[str]=None) -> None:
    """Persist a failed run on the state row, in its own transaction.

    The sync's own transaction is being rolled back, so the failure has to be
    written after that; otherwise it would be discarded along with the partial
    work it is describing. Never raises: losing the error record must not
    replace the original exception with a less useful one.
    """
    try:
        await db_session.rollback()
        state = await _load_state(db_session)
        overtaken = state.state == DemoStateEnum.READY and (started_from is None or state.update_date != started_from)
        state.last_error = f'{type(exc).__name__}: {exc}'[:1000]
        if overtaken:
            logger.warning('Demo sync failed, but another run has since succeeded; recording the error without marking the demo failed')
        else:
            state.state = DemoStateEnum.FAILED
        state.update_date = _now()
        db_session.add(state)
        await db_session.commit()
    except Exception:
        logger.exception('Could not record the demo sync failure')

async def _sync_demo_inner(db_session: AsyncSession, *, today: Optional[date]=None) -> SyncStats:
    bundle = get_bundle()
    stats = SyncStats()
    state = await _load_state(db_session)
    reprovision = state.bundle_version != bundle.manifest.bundle_version
    epoch = _resolve_epoch(state, today=today)
    stats.epoch = epoch
    org, created_org = await _sync_org(db_session, bundle, stats)
    stats.provisioned = created_org or reprovision
    registry = await Registry.load(org.id, db_session)
    await _sync_branding(db_session, bundle, org, registry, stats)
    students = plan_students(bundle, epoch)
    users_by_key = await _sync_students(db_session, bundle, org, registry, students, stats)
    await _sync_cohorts(db_session, bundle, org, registry, students, users_by_key, stats)
    courses_by_key = await _sync_courses(db_session, bundle, org, registry, epoch, stats)
    await _sync_assignments(db_session, bundle, org, registry, epoch, stats)
    await _sync_certifications(db_session, bundle, org, registry, courses_by_key, stats)
    await _sync_engagement(db_session, bundle, org, registry, epoch, users_by_key, stats)
    await db_session.flush()
    forgotten = await registry.prune_removed_from_bundle(db_session)
    if forgotten:
        logger.info('Demo: forgot %s entry(ies) dropped from the bundle', forgotten)
    await _delete_drift(db_session, org, registry, stats)
    await _sync_progress(db_session, bundle, org, registry, epoch, students, users_by_key, stats)
    await _assert_single_tenant(db_session, org)
    await _finalize(db_session, state, org, bundle, epoch, stats)
    await db_session.commit()
    return stats

async def _load_state(db_session: AsyncSession) -> DemoState:
    state = (await db_session.execute(select(DemoState).where(DemoState.id == DEMO_STATE_ID))).scalars().first()
    if state is None:
        state = DemoState(id=DEMO_STATE_ID, state=DemoStateEnum.PROVISIONING, creation_date=_now(), update_date=_now())
        db_session.add(state)
    return state

def _resolve_epoch(state: DemoState, today: Optional[date]=None) -> str:
    """The date anchor for all generated timestamps.

    Rolls once a day. Hourly refreshes reuse the stored value and therefore
    recompute identical timestamps; if this returned today's date unconditionally
    it would still only change daily, but reading it from state keeps the roll
    explicit and testable.
    """
    current = today or datetime.utcnow().date()
    if not state.content_epoch:
        return epoch_for(current)
    stored = date.fromisoformat(state.content_epoch)
    if stored < current:
        return epoch_for(current)
    return state.content_epoch

async def _sync_org(db_session: AsyncSession, bundle: Bundle, stats: SyncStats) -> tuple[Organization, bool]:
    stats.steps.append('org')
    slug = flags.demo_slug()
    spec = bundle.manifest.org
    org = (await db_session.execute(select(Organization).where(Organization.slug == slug))).scalars().first()
    created = False
    if org is None:
        org = Organization(name=spec.name, description=spec.description, about=spec.about, email=spec.email, slug=slug, org_uuid=_prefixed('org'), is_demo=True, explore=False, creation_date=_now(), update_date=_now())
        db_session.add(org)
        await db_session.flush()
        created = True
        stats.created += 1
    else:
        if not org.is_demo:
            raise DemoSlugTakenError(f'organization {slug!r} already exists and is not a demo org; set LEARNHOUSE_DEMO_SLUG to something else')
        if _apply(org, name=spec.name, description=spec.description, about=spec.about, email=spec.email, explore=False, scripts={}, previews={}):
            org.update_date = _now()
            db_session.add(org)
            stats.updated += 1
    config = (await db_session.execute(select(OrganizationConfig).where(OrganizationConfig.org_id == org.id))).scalars().first()
    desired = _demo_org_config()
    if config is None:
        db_session.add(OrganizationConfig(org_id=org.id, config=desired, creation_date=_now(), update_date=_now()))
        stats.created += 1
    elif config.config != desired:
        config.config = desired
        config.update_date = _now()
        db_session.add(config)
        stats.updated += 1
    return (org, created)
_UPLOAD_NAMESPACE = UUID('2b7c1f52-9a44-5c6e-8d31-70e5a4c9b18f')

def _upload_name(*, uuid: str, directory: str, name: str, version: str='') -> str:
    """The filename a bundle asset always lands on, derived rather than drawn.

    A random name would be unknowable to a later refresh, which is what let a
    visitor's replacement image stick: the sync could only ask "is this column
    empty?", and an overwritten column is not empty. Deriving the name makes
    the correct value computable, so the column can be reverted like any other
    bundle-managed field.

    The bundle version is part of the key, which is how new artwork reaches an
    existing demo. Every media write is guarded by "does the column already
    name this file?", so without the version a redrawn logo shipped under the
    same bundle filename would be computed to the same name, match, and never
    upload. Bumping bundle_version changes every derived name, so each asset is
    written once more and every reference moves with it.
    """
    key = f'{uuid}:{directory}:{name}:{version}'
    return f'{uuid5(_UPLOAD_NAMESPACE, key)}_{name}'

async def _upload(*, directory: str, type_of_dir: str, uuid: str, source_path: str, name: str, version: str='', allowed_formats: Optional[list[str]]=None) -> str:
    """Copy a bundle image into org/user storage through the normal funnel.

    Uses upload_content rather than upload_file because there is no UploadFile
    here (just bytes on disk), and the same funnel handles both the filesystem
    and S3 backends.
    """
    from src.services.utils.upload_content import upload_content
    with open(source_path, 'rb') as handle:
        payload = handle.read()
    filename = _upload_name(uuid=uuid, directory=directory, name=name, version=version)
    await upload_content(directory=directory, type_of_dir=type_of_dir, uuid=uuid, file_binary=payload, file_and_format=filename, allowed_formats=allowed_formats or ['webp', 'png', 'jpg', 'jpeg'])
    return filename

async def _sync_branding(db_session: AsyncSession, bundle: Bundle, org: Organization, registry: Registry, stats: SyncStats) -> None:
    """Put the org logo and banner back to the bundle's, uploading when needed.

    The column is compared against the filename the bundle asset derives, not
    merely checked for emptiness. Emptiness was the wrong question: a visitor
    who uploads their own logo through the org settings leaves a non-empty
    column, so the demo kept their branding permanently: the one piece of the
    org every prospect sees first.

    The upload itself still happens only when the column does not already name
    the derived file, so a settled demo writes nothing and S3 gains no orphans.
    """
    stats.steps.append('branding')
    changed = False
    for column, directory, source, name in (('logo_image', 'logos', bundle.manifest.org.logo, 'logo.webp'), ('thumbnail_image', 'thumbnails', bundle.manifest.org.thumbnail, 'thumbnail.webp')):
        expected = _upload_name(uuid=org.org_uuid, directory=directory, name=name, version=bundle.manifest.bundle_version)
        if getattr(org, column) == expected:
            continue
        setattr(org, column, await _upload(directory=directory, type_of_dir='orgs', uuid=org.org_uuid, source_path=bundle.media_path(source), name=name, version=bundle.manifest.bundle_version))
        changed = True
    if changed:
        org.update_date = _now()
        db_session.add(org)
        stats.updated += 1

async def _sync_students(db_session: AsyncSession, bundle: Bundle, org: Organization, registry: Registry, students: list[StudentPlan], stats: SyncStats) -> dict[str, User]:
    stats.steps.append('students')
    users: dict[str, User] = {}
    for plan in students:
        entry = registry.get(DemoEntityKind.USER, plan.bundle_key)
        user: Optional[User] = None
        if entry is not None:
            user = (await db_session.execute(select(User).where(User.id == entry.entity_id))).scalars().first()
        if user is None:
            user = User(username=plan.username, first_name=plan.first_name, last_name=plan.last_name, email=plan.email, password=_unusable_password_hash(), user_uuid=entry.entity_uuid if entry else _prefixed('user'), email_verified=True, email_verified_at=_now(), signup_method=flags.DEMO_SIGNUP_METHOD, creation_date=_days_ago(stats.epoch, plan.joined_days_ago), update_date=_now())
            db_session.add(user)
            await db_session.flush()
            stats.created += 1
        elif _apply(user, first_name=plan.first_name, last_name=plan.last_name, email=plan.email, username=plan.username, signup_method=flags.DEMO_SIGNUP_METHOD):
            user.update_date = _now()
            db_session.add(user)
            stats.updated += 1
        if plan.avatar:
            expected_avatar = _upload_name(uuid=user.user_uuid, directory='avatars', name='avatar.webp', version=bundle.manifest.bundle_version)
            if user.avatar_image != expected_avatar:
                user.avatar_image = await _upload(directory='avatars', type_of_dir='users', uuid=user.user_uuid, source_path=bundle.media_path('avatars', plan.avatar), name='avatar.webp', version=bundle.manifest.bundle_version)
                db_session.add(user)
                stats.updated += 1
        elif user.avatar_image:
            user.avatar_image = ''
            db_session.add(user)
            stats.updated += 1
        registry.register(db_session, kind=DemoEntityKind.USER, bundle_key=plan.bundle_key, entity_id=user.id, entity_uuid=user.user_uuid, org_id=org.id)
        users[plan.bundle_key] = user
        membership = (await db_session.execute(select(UserOrganization).where(UserOrganization.user_id == user.id, UserOrganization.org_id == org.id))).scalars().first()
        if membership is None:
            db_session.add(UserOrganization(user_id=user.id, org_id=org.id, role_id=LEARNER_ROLE_ID, creation_date=_days_ago(stats.epoch, plan.joined_days_ago), update_date=_now()))
            stats.created += 1
    return users

async def _sync_cohorts(db_session: AsyncSession, bundle: Bundle, org: Organization, registry: Registry, students: list[StudentPlan], users: dict[str, User], stats: SyncStats) -> None:
    stats.steps.append('cohorts')
    for cohort in bundle.manifest.cohorts:
        entry = registry.get(DemoEntityKind.USERGROUP, cohort.slug)
        group: Optional[UserGroup] = None
        if entry is not None:
            group = (await db_session.execute(select(UserGroup).where(UserGroup.id == entry.entity_id))).scalars().first()
        if group is None:
            group = UserGroup(name=cohort.name, description=cohort.description, org_id=org.id, usergroup_uuid=entry.entity_uuid if entry else _prefixed('usergroup'), creation_date=_now(), update_date=_now())
            db_session.add(group)
            await db_session.flush()
            stats.created += 1
        elif _apply(group, name=cohort.name, description=cohort.description):
            group.update_date = _now()
            db_session.add(group)
            stats.updated += 1
        registry.register(db_session, kind=DemoEntityKind.USERGROUP, bundle_key=cohort.slug, entity_id=group.id, entity_uuid=group.usergroup_uuid, org_id=org.id)
        wanted_user_ids = {users[plan.bundle_key].id for plan in students if plan.cohort_key == cohort.slug and plan.bundle_key in users}
        existing = (await db_session.execute(select(UserGroupUser).where(UserGroupUser.usergroup_id == group.id))).scalars().all()
        existing_ids = {row.user_id for row in existing}
        for user_id in wanted_user_ids - existing_ids:
            db_session.add(UserGroupUser(usergroup_id=group.id, user_id=user_id, org_id=org.id, creation_date=_now(), update_date=_now()))
            stats.created += 1
        stale = [row for row in existing if row.user_id not in wanted_user_ids]
        if stale:
            await db_session.execute(delete(UserGroupUser).where(UserGroupUser.id.in_([row.id for row in stale])))
            stats.updated += len(stale)

async def _sync_courses(db_session: AsyncSession, bundle: Bundle, org: Organization, registry: Registry, epoch: str, stats: SyncStats) -> dict[str, Course]:
    stats.steps.append('courses')
    courses: dict[str, Course] = {}
    for course_spec in bundle.courses:
        entry = registry.get(DemoEntityKind.COURSE, course_spec.slug)
        course: Optional[Course] = None
        if entry is not None:
            course = (await db_session.execute(select(Course).where(Course.id == entry.entity_id))).scalars().first()
        if course is None:
            course = Course(name=course_spec.name, description=course_spec.description, about=course_spec.about, learnings=course_spec.learnings, tags=course_spec.tags, thumbnail_type=ThumbnailType.IMAGE, thumbnail_image='', thumbnail_video='', public=course_spec.public, published=course_spec.published, open_to_contributors=False, org_id=org.id, course_uuid=entry.entity_uuid if entry else _prefixed('course'), creation_date=_days_ago(epoch, HISTORY_DAYS + 30), update_date=_now())
            db_session.add(course)
            await db_session.flush()
            stats.created += 1
        elif _apply(course, name=course_spec.name, description=course_spec.description, about=course_spec.about, learnings=course_spec.learnings, tags=course_spec.tags, public=course_spec.public, published=course_spec.published, thumbnail_type=ThumbnailType.IMAGE, thumbnail_video=''):
            course.update_date = _now()
            db_session.add(course)
            stats.updated += 1
        thumbnail_dir = f'courses/{course.course_uuid}/thumbnails'
        expected_thumbnail = _upload_name(uuid=org.org_uuid, directory=thumbnail_dir, name='thumbnail.webp', version=bundle.manifest.bundle_version)
        if course.thumbnail_image != expected_thumbnail:
            course.thumbnail_image = await _upload(directory=thumbnail_dir, type_of_dir='orgs', uuid=org.org_uuid, source_path=bundle.media_path('courses', course_spec.thumbnail), name='thumbnail.webp', version=bundle.manifest.bundle_version)
            db_session.add(course)
            stats.updated += 1
        registry.register(db_session, kind=DemoEntityKind.COURSE, bundle_key=course_spec.slug, entity_id=course.id, entity_uuid=course.course_uuid, org_id=org.id)
        courses[course_spec.slug] = course
        await _sync_resource_author(db_session, course, org, stats)
        await _sync_chapters(db_session, course_spec, course, org, registry, epoch, stats)
    return courses

async def _sync_resource_author(db_session: AsyncSession, course: Course, org: Organization, stats: SyncStats) -> None:
    await _sync_resource_author_for(db_session, course.course_uuid, org, stats)

async def _sync_resource_author_for(db_session: AsyncSession, resource_uuid: str, org: Organization, stats: SyncStats) -> None:
    """Attribute a resource to one of the seeded demo people.

    Courses and podcasts without an author render with an empty byline.

    Deliberately a demo student rather than "the org's first admin": the sync
    never creates an admin, so the first admin is the first *visitor*, and
    attributing every course and podcast to them publishes a real person's name
    and email as the author of content they have never seen. The byline belongs
    to a fictional person because the content is fictional.
    """
    admin_id = (await db_session.execute(select(UserOrganization.user_id).join(User, User.id == UserOrganization.user_id).where(UserOrganization.org_id == org.id, User.signup_method == flags.DEMO_SIGNUP_METHOD).order_by(UserOrganization.id).limit(1))).scalars().first()
    if admin_id is None:
        return
    rows = (await db_session.execute(select(ResourceAuthor).where(ResourceAuthor.resource_uuid == resource_uuid))).scalars().all()
    existing = next((row for row in rows if row.user_id == admin_id), None)
    if existing is None:
        db_session.add(ResourceAuthor(resource_uuid=resource_uuid, user_id=admin_id, authorship=ResourceAuthorshipEnum.CREATOR, authorship_status=ResourceAuthorshipStatusEnum.ACTIVE, creation_date=_now(), update_date=_now()))
        stats.created += 1
    intruders = [row.id for row in rows if row.user_id != admin_id and row.id]
    if intruders:
        await db_session.execute(delete(ResourceAuthor).where(ResourceAuthor.id.in_(intruders)))
        stats.drift_deleted += len(intruders)

async def _sync_chapters(db_session: AsyncSession, course_spec: Any, course: Course, org: Organization, registry: Registry, epoch: str, stats: SyncStats) -> None:
    for order, chapter_spec in enumerate(course_spec.chapters):
        entry = registry.get(DemoEntityKind.CHAPTER, chapter_spec.slug)
        chapter: Optional[Chapter] = None
        if entry is not None:
            chapter = (await db_session.execute(select(Chapter).where(Chapter.id == entry.entity_id))).scalars().first()
        if chapter is None:
            chapter = Chapter(name=chapter_spec.name, description=chapter_spec.description, thumbnail_image='', org_id=org.id, course_id=course.id, chapter_uuid=entry.entity_uuid if entry else _prefixed('chapter'), creation_date=_days_ago(epoch, HISTORY_DAYS + 28), update_date=_now())
            db_session.add(chapter)
            await db_session.flush()
            stats.created += 1
        elif _apply(chapter, name=chapter_spec.name, description=chapter_spec.description):
            chapter.update_date = _now()
            db_session.add(chapter)
            stats.updated += 1
        registry.register(db_session, kind=DemoEntityKind.CHAPTER, bundle_key=chapter_spec.slug, entity_id=chapter.id, entity_uuid=chapter.chapter_uuid, org_id=org.id)
        link = (await db_session.execute(select(CourseChapter).where(CourseChapter.course_id == course.id, CourseChapter.chapter_id == chapter.id))).scalars().first()
        if link is None:
            db_session.add(CourseChapter(course_id=course.id, chapter_id=chapter.id, org_id=org.id, order=order, creation_date=_now(), update_date=_now()))
            stats.created += 1
        elif _apply(link, order=order):
            link.update_date = _now()
            db_session.add(link)
            stats.updated += 1
        await _sync_activities(db_session, chapter_spec, chapter, course, org, registry, epoch, stats)
_ACTIVITY_TYPES = {'document': (ActivityTypeEnum.TYPE_DYNAMIC, ActivitySubTypeEnum.SUBTYPE_DYNAMIC_PAGE), 'video': (ActivityTypeEnum.TYPE_VIDEO, ActivitySubTypeEnum.SUBTYPE_VIDEO_YOUTUBE), 'assignment': (ActivityTypeEnum.TYPE_ASSIGNMENT, ActivitySubTypeEnum.SUBTYPE_ASSIGNMENT_ANY)}

async def _sync_activities(db_session: AsyncSession, chapter_spec: Any, chapter: Chapter, course: Course, org: Organization, registry: Registry, epoch: str, stats: SyncStats) -> None:
    for order, activity_spec in enumerate(chapter_spec.activities):
        activity_type, activity_sub_type = _ACTIVITY_TYPES[activity_spec.kind]
        content = activity_spec.compiled_content()
        if activity_spec.kind == 'video':
            content = {'youtube_id': activity_spec.youtube_id}
        entry = registry.get(DemoEntityKind.ACTIVITY, activity_spec.slug)
        activity: Optional[Activity] = None
        if entry is not None:
            activity = (await db_session.execute(select(Activity).where(Activity.id == entry.entity_id))).scalars().first()
        if activity is None:
            activity = Activity(name=activity_spec.name, activity_type=activity_type, activity_sub_type=activity_sub_type, content=content, published=activity_spec.published, org_id=org.id, course_id=course.id, activity_uuid=entry.entity_uuid if entry else _prefixed('activity'), creation_date=_days_ago(epoch, HISTORY_DAYS + 25), update_date=_now())
            db_session.add(activity)
            await db_session.flush()
            stats.created += 1
        elif _apply(activity, name=activity_spec.name, content=content, published=activity_spec.published, activity_type=activity_type, activity_sub_type=activity_sub_type):
            activity.update_date = _now()
            db_session.add(activity)
            stats.updated += 1
        registry.register(db_session, kind=DemoEntityKind.ACTIVITY, bundle_key=activity_spec.slug, entity_id=activity.id, entity_uuid=activity.activity_uuid, org_id=org.id)
        link = (await db_session.execute(select(ChapterActivity).where(ChapterActivity.chapter_id == chapter.id, ChapterActivity.activity_id == activity.id))).scalars().first()
        if link is None:
            db_session.add(ChapterActivity(chapter_id=chapter.id, activity_id=activity.id, course_id=course.id, org_id=org.id, order=order, creation_date=_now(), update_date=_now()))
            stats.created += 1
        elif _apply(link, order=order):
            link.update_date = _now()
            db_session.add(link)
            stats.updated += 1

async def _sync_assignments(db_session: AsyncSession, bundle: Bundle, org: Organization, registry: Registry, epoch: str, stats: SyncStats) -> None:
    stats.steps.append('assignments')
    for course_key, sidecar in bundle.assignments.items():
        activity_entry = registry.get(DemoEntityKind.ACTIVITY, sidecar['activity_key'])
        course_entry = registry.get(DemoEntityKind.COURSE, course_key)
        if activity_entry is None or course_entry is None:
            continue
        activity = (await db_session.execute(select(Activity).where(Activity.id == activity_entry.entity_id))).scalars().first()
        if activity is None:
            continue
        chapter_link = (await db_session.execute(select(ChapterActivity).where(ChapterActivity.activity_id == activity.id))).scalars().first()
        if chapter_link is None:
            continue
        due_date = _days_ago(epoch, -float(sidecar.get('due_in_days', 7)))
        entry = registry.get(DemoEntityKind.ASSIGNMENT, course_key)
        assignment: Optional[Assignment] = None
        if entry is not None:
            assignment = (await db_session.execute(select(Assignment).where(Assignment.id == entry.entity_id))).scalars().first()
        fields = dict(title=sidecar['title'], description=sidecar['description'], due_date=due_date, published=sidecar.get('published', True), grading_type=sidecar.get('grading_type', 'PERCENTAGE'), auto_grading=sidecar.get('auto_grading', True), show_correct_answers=sidecar.get('show_correct_answers', True), allow_retries=sidecar.get('allow_retries', True), max_retries=sidecar.get('max_retries', 2), pass_threshold_percentage=sidecar.get('pass_threshold_percentage', 60))
        if assignment is None:
            assignment = Assignment(assignment_uuid=entry.entity_uuid if entry else _prefixed('assignment'), org_id=org.id, course_id=activity.course_id, chapter_id=chapter_link.chapter_id, activity_id=activity.id, creation_date=_days_ago(epoch, HISTORY_DAYS), update_date=_now(), **fields)
            db_session.add(assignment)
            await db_session.flush()
            stats.created += 1
        elif _apply(assignment, **fields):
            assignment.update_date = _now()
            db_session.add(assignment)
            stats.updated += 1
        registry.register(db_session, kind=DemoEntityKind.ASSIGNMENT, bundle_key=course_key, entity_id=assignment.id, entity_uuid=assignment.assignment_uuid, org_id=org.id)
        for task_spec in sidecar['tasks']:
            await _sync_assignment_task(db_session, task_spec, assignment, activity, chapter_link, org, registry, epoch, stats)

async def _sync_assignment_task(db_session: AsyncSession, task_spec: dict, assignment: Assignment, activity: Activity, chapter_link: ChapterActivity, org: Organization, registry: Registry, epoch: str, stats: SyncStats) -> None:
    entry = registry.get(DemoEntityKind.ASSIGNMENT_TASK, task_spec['slug'])
    task: Optional[AssignmentTask] = None
    if entry is not None:
        task = (await db_session.execute(select(AssignmentTask).where(AssignmentTask.id == entry.entity_id))).scalars().first()
    fields = dict(title=task_spec['title'], description=task_spec.get('description', ''), hint=task_spec.get('hint', ''), assignment_type=task_spec['type'], contents=task_spec['contents'], max_grade_value=task_spec['max_grade_value'])
    if task is None:
        task = AssignmentTask(assignment_task_uuid=entry.entity_uuid if entry else _prefixed('assignmenttask'), reference_file='', assignment_id=assignment.id, org_id=org.id, course_id=activity.course_id, chapter_id=chapter_link.chapter_id, activity_id=activity.id, creation_date=_days_ago(epoch, HISTORY_DAYS), update_date=_now(), **fields)
        db_session.add(task)
        await db_session.flush()
        stats.created += 1
    elif _apply(task, **fields):
        task.update_date = _now()
        db_session.add(task)
        stats.updated += 1
    registry.register(db_session, kind=DemoEntityKind.ASSIGNMENT_TASK, bundle_key=task_spec['slug'], entity_id=task.id, entity_uuid=task.assignment_task_uuid, org_id=org.id)

async def _sync_certifications(db_session: AsyncSession, bundle: Bundle, org: Organization, registry: Registry, courses: dict[str, Course], stats: SyncStats) -> None:
    stats.steps.append('certifications')
    for course_spec in bundle.courses:
        if not course_spec.certification:
            continue
        course = courses.get(course_spec.slug)
        if course is None:
            continue
        entry = registry.get(DemoEntityKind.CERTIFICATION, course_spec.slug)
        certification: Optional[Certifications] = None
        if entry is not None:
            certification = (await db_session.execute(select(Certifications).where(Certifications.id == entry.entity_id))).scalars().first()
        desired_config = {'certification_name': course_spec.certification_name or course_spec.name, 'certification_description': course_spec.certification_description or f'Awarded in recognition of completing {course_spec.name} at {bundle.manifest.org.name}.', 'certification_type': course_spec.certification_type, 'certificate_pattern': course_spec.certificate_pattern, 'certificate_instructor': bundle.manifest.org.name}
        if certification is None:
            certification = Certifications(certification_uuid=entry.entity_uuid if entry else _prefixed('certification'), course_id=course.id, config=desired_config, creation_date=_now(), update_date=_now())
            db_session.add(certification)
            await db_session.flush()
            stats.created += 1
        elif _apply(certification, config=desired_config):
            certification.update_date = _now()
            db_session.add(certification)
            stats.updated += 1
        registry.register(db_session, kind=DemoEntityKind.CERTIFICATION, bundle_key=course_spec.slug, entity_id=certification.id, entity_uuid=certification.certification_uuid, org_id=org.id)

async def _sync_engagement(db_session: AsyncSession, bundle: Bundle, org: Organization, registry: Registry, epoch: str, users: dict[str, User], stats: SyncStats) -> None:
    """Seed the community, boards and playgrounds.

    The plan enables these features; this is what makes them worth looking at.
    An enabled Boards page with nothing on it demonstrates less than a disabled
    one; a prospect reads "empty" as "nobody uses this".

    Podcasts are deliberately absent: an episode needs a real audio file, which
    carries the same repo-weight and transcoding cost as shipping video.
    """
    stats.steps.append('engagement')
    engagement = bundle.engagement
    if engagement.community:
        await _sync_community(db_session, engagement.community, org, registry, epoch, users, stats)

async def _sync_community(db_session: AsyncSession, spec: Any, org: Organization, registry: Registry, epoch: str, users: dict[str, User], stats: SyncStats) -> None:
    from src.db.communities.communities import Community
    from src.db.communities.discussion_comments import DiscussionComment
    from src.db.communities.discussions import Discussion
    entry = registry.get(DemoEntityKind.COMMUNITY, spec.slug)
    community: Optional[Community] = None
    if entry is not None:
        community = (await db_session.execute(select(Community).where(Community.id == entry.entity_id))).scalars().first()
    if community is None:
        community = Community(name=spec.name, description=spec.description, public=spec.public, org_id=org.id, community_uuid=entry.entity_uuid if entry else _prefixed('community'), creation_date=_days_ago(epoch, HISTORY_DAYS + 10), update_date=_now())
        db_session.add(community)
        await db_session.flush()
        stats.created += 1
    elif _apply(community, name=spec.name, description=spec.description, public=spec.public):
        community.update_date = _now()
        db_session.add(community)
        stats.updated += 1
    registry.register(db_session, kind=DemoEntityKind.COMMUNITY, bundle_key=spec.slug, entity_id=community.id, entity_uuid=community.community_uuid, org_id=org.id)
    for discussion_spec in spec.discussions:
        author = users.get(discussion_spec.author)
        if author is None:
            continue
        d_entry = registry.get(DemoEntityKind.DISCUSSION, discussion_spec.slug)
        discussion: Optional[Discussion] = None
        if d_entry is not None:
            discussion = (await db_session.execute(select(Discussion).where(Discussion.id == d_entry.entity_id))).scalars().first()
        fields = dict(title=discussion_spec.title, content=discussion_spec.content, label=discussion_spec.label, emoji=discussion_spec.emoji, upvote_count=discussion_spec.upvotes, is_pinned=discussion_spec.pinned, is_locked=False)
        if discussion is None:
            discussion = Discussion(community_id=community.id, org_id=org.id, author_id=author.id, discussion_uuid=d_entry.entity_uuid if d_entry else _prefixed('discussion'), creation_date=_days_ago(epoch, discussion_spec.days_ago), update_date=_days_ago(epoch, discussion_spec.days_ago), **fields)
            db_session.add(discussion)
            await db_session.flush()
            stats.created += 1
        elif _apply(discussion, **fields):
            db_session.add(discussion)
            stats.updated += 1
        registry.register(db_session, kind=DemoEntityKind.DISCUSSION, bundle_key=discussion_spec.slug, entity_id=discussion.id, entity_uuid=discussion.discussion_uuid, org_id=org.id)
        for index, comment_spec in enumerate(discussion_spec.comments):
            commenter = users.get(comment_spec.author)
            if commenter is None:
                continue
            comment_key = f'{discussion_spec.slug}-c{index}'
            c_entry = registry.get(DemoEntityKind.DISCUSSION_COMMENT, comment_key)
            comment: Optional[DiscussionComment] = None
            if c_entry is not None:
                comment = (await db_session.execute(select(DiscussionComment).where(DiscussionComment.id == c_entry.entity_id))).scalars().first()
            if comment is None:
                comment = DiscussionComment(discussion_id=discussion.id, author_id=commenter.id, content=comment_spec.content, upvote_count=comment_spec.upvotes, comment_uuid=c_entry.entity_uuid if c_entry else _prefixed('discussioncomment'), creation_date=_days_ago(epoch, comment_spec.days_ago), update_date=_days_ago(epoch, comment_spec.days_ago))
                db_session.add(comment)
                await db_session.flush()
                stats.created += 1
            elif _apply(comment, content=comment_spec.content, upvote_count=comment_spec.upvotes):
                db_session.add(comment)
                stats.updated += 1
            registry.register(db_session, kind=DemoEntityKind.DISCUSSION_COMMENT, bundle_key=comment_key, entity_id=comment.id, entity_uuid=comment.comment_uuid, org_id=org.id)

async def _delete_drift(db_session: AsyncSession, org: Organization, registry: Registry, stats: SyncStats) -> None:
    """Remove anything in the demo org the bundle does not own.

    That is, by definition, whatever a visitor created. Their membership of the
    org is left alone: visitors are not drift, their content is.
    """
    stats.steps.append('drift')
    removals: list[_Sweep] = [_Sweep(Activity, Activity.activity_uuid, DemoEntityKind.ACTIVITY), _Sweep(Chapter, Chapter.chapter_uuid, DemoEntityKind.CHAPTER), _Sweep(Course, Course.course_uuid, DemoEntityKind.COURSE), _Sweep(UserGroup, UserGroup.usergroup_uuid, DemoEntityKind.USERGROUP), _Sweep(Assignment, Assignment.assignment_uuid, DemoEntityKind.ASSIGNMENT), _Sweep(AssignmentTask, AssignmentTask.assignment_task_uuid, DemoEntityKind.ASSIGNMENT_TASK), _Sweep(Community, Community.community_uuid, DemoEntityKind.COMMUNITY), _Sweep(Discussion, Discussion.discussion_uuid, DemoEntityKind.DISCUSSION), _Sweep(APIToken, APIToken.token_uuid, DemoEntityKind.API_TOKEN), _Sweep(WebhookEndpoint, WebhookEndpoint.webhook_uuid, DemoEntityKind.WEBHOOK), _Sweep(Block, Block.block_uuid, DemoEntityKind.BLOCK)]
    for sweep in removals:
        model, uuid_column, kind = (sweep.model, sweep.uuid_column, sweep.kind)
        owned = registry.uuids_of_kind(kind)
        stale_uuids = (await db_session.execute(select(uuid_column).where(model.org_id == org.id, uuid_column.notin_(owned) if owned else True))).scalars().all()
        if not stale_uuids:
            continue
        await db_session.execute(delete(ResourceAuthor).where(ResourceAuthor.resource_uuid.in_(stale_uuids)))
        await db_session.execute(delete(model).where(model.org_id == org.id, uuid_column.in_(stale_uuids)))
        for stale_uuid in _storage_paths_for(kind, org, stale_uuids):
            _remove_storage(stale_uuid)
        stats.drift_deleted += len(stale_uuids)
        logger.info('Demo drift: removed %s %s row(s)', len(stale_uuids), sweep.kind)
    await _delete_comment_drift(db_session, org, registry, stats)
    await _delete_reaction_drift(db_session, org, stats)
    await _delete_unowned_org_drift(db_session, org, registry, stats)
    await _delete_certification_drift(db_session, org, registry, stats)
    await _delete_visitor_progress_drift(db_session, org, registry, stats)

def _storage_paths_for(kind: str, org: Organization, uuids) -> list[str]:
    """Storage directories to reclaim for a set of drift rows of one kind.

    Only kinds that own a directory of their own appear here. An activity's
    media lives under its course and an episode's under its podcast, so those
    are reclaimed with the parent when the parent is drift, and correctly kept
    when the parent is bundle-owned.
    """
    root = f'content/orgs/{org.org_uuid}'
    if kind == DemoEntityKind.COURSE:
        return [f'{root}/courses/{uuid}' for uuid in uuids]
    return []

def _remove_storage(path: str) -> None:
    """Delete a storage directory, never letting the failure abort the sync.

    An unreachable bucket should cost orphaned files and a warning, not a
    half-finished refresh that leaves the demo vandalised until the next tick.
    """
    try:
        from src.services.courses.transfer.storage_utils import delete_storage_directory
        delete_storage_directory(path)
    except Exception:
        logger.warning('Demo drift: could not remove %s', path, exc_info=True)

async def _delete_unowned_org_drift(db_session: AsyncSession, org: Organization, registry: Registry, stats: SyncStats) -> None:
    """Sweep org-scoped tables the bundle never writes to.

    These carry an org_id but no uuid column, so they cannot join the
    uuid-matched sweep above, and since the sync creates none of them, every
    row in the demo org belongs to a visitor.

    Activity versions are the reason this is not merely tidiness: the sync puts
    activity content back, but each intermediate version a visitor saved was
    kept, carrying their name and a one-click restore.

    Note what is *not* swept here. Audit events for logins and logouts carry a
    NULL org_id by design (a connection is not an organization's event), so
    they are not the demo's rows to delete, and a sweep matching NULL would be
    writing outside the demo org. The dossier that exposed them is scoped in
    the EE audit router instead, which is where the leak actually was.
    """
    from src.security.rbac.constants import ADMIN_ROLE_ID
    for model, label in ((UserAuditEvent, 'audit event'), (ActivityVersion, 'activity version')):
        stale = (await db_session.execute(select(model.id).where(model.org_id == org.id))).scalars().all()
        if not stale:
            continue
        await db_session.execute(delete(model).where(model.id.in_(stale)))
        stats.drift_deleted += len(stale)
        logger.info('Demo drift: removed %s %s row(s)', len(stale), label)
    custom_role_ids = (await db_session.execute(select(Role.id).where(Role.org_id == org.id))).scalars().all()
    if not custom_role_ids:
        return
    demo_user_ids = registry.ids_of_kind(DemoEntityKind.USER)
    memberships = (await db_session.execute(select(UserOrganization).where(UserOrganization.org_id == org.id, UserOrganization.role_id.in_(custom_role_ids)))).scalars().all()
    for membership in memberships:
        membership.role_id = LEARNER_ROLE_ID if membership.user_id in demo_user_ids else ADMIN_ROLE_ID
        db_session.add(membership)
    await db_session.flush()
    still_referenced = set((await db_session.execute(select(UserOrganization.role_id).where(UserOrganization.role_id.in_(custom_role_ids)))).scalars().all())
    deletable = [rid for rid in custom_role_ids if rid not in still_referenced]
    if not deletable:
        logger.warning('Demo drift: %s custom role(s) are still assigned outside the demo org and were left in place', len(custom_role_ids))
        return
    await db_session.execute(delete(Role).where(Role.id.in_(deletable)))
    stats.drift_deleted += len(deletable)
    logger.info('Demo drift: removed %s custom role(s)', len(deletable))

async def _delete_reaction_drift(db_session: AsyncSession, org: Organization, stats: SyncStats) -> None:
    """Remove every vote and reaction inside the demo.

    The bundle seeds none of these (discussion upvote counts are a column on
    the discussion, not rows), so anything here was left by a visitor. They
    hang off a discussion, a comment or a playground rather than carrying an
    org_id, so the generic uuid sweep cannot see them at all, and their parents
    are bundle-owned and survive every refresh.

    Not cosmetic: the reactions API returns the reacting user's username, real
    name and avatar, so a visitor's identity was published under a seeded
    discussion for every prospect who came after them.
    """
    org_discussions = select(Discussion.id).join(Community, Community.id == Discussion.community_id).where(Community.org_id == org.id)
    org_comments = select(DiscussionComment.id).where(DiscussionComment.discussion_id.in_(org_discussions))
    sweeps = [(DiscussionVote, DiscussionVote.discussion_id.in_(org_discussions), 'discussion vote'), (DiscussionReaction, DiscussionReaction.discussion_id.in_(org_discussions), 'discussion reaction'), (DiscussionCommentVote, DiscussionCommentVote.comment_id.in_(org_comments), 'comment vote')]
    for model, in_org, label in sweeps:
        stale = (await db_session.execute(select(model.id).where(in_org))).scalars().all()
        if not stale:
            continue
        await db_session.execute(delete(model).where(model.id.in_(stale)))
        stats.drift_deleted += len(stale)
        logger.info('Demo drift: removed %s %s row(s)', len(stale), label)

async def _delete_visitor_progress_drift(db_session: AsyncSession, org: Organization, registry: Registry, stats: SyncStats) -> None:
    """Remove the learning history a visitor leaves behind in the demo.

    Distinct from _delete_progress_drift, which reconciles the *seeded*
    learners against the current epoch and deliberately scopes itself to them.
    Nothing removed a visitor's own trail, submissions or certificate, so a
    prospect who tried a course appeared in the learners table, the grading
    inbox and the certificate list of the next prospect, who is also an admin
    and can read the lot, under that person's real name.

    Every table here scopes through a parent rather than an org_id of its own,
    which is why the generic uuid sweep cannot reach any of them.
    """
    demo_user_ids = registry.ids_of_kind(DemoEntityKind.USER)
    if not demo_user_ids:
        return
    org_courses = select(Course.id).where(Course.org_id == org.id)
    org_assignments = select(Assignment.id).where(Assignment.org_id == org.id)
    org_certifications = select(Certifications.id).where(Certifications.course_id.in_(org_courses))
    sweeps = [(Trail, Trail.org_id == org.id, 'trail'), (TrailRun, TrailRun.org_id == org.id, 'trail run'), (UserActivityDay, UserActivityDay.org_id == org.id, 'activity day'), (AssignmentUserSubmission, AssignmentUserSubmission.assignment_id.in_(org_assignments), 'assignment submission'), (AssignmentTaskSubmission, AssignmentTaskSubmission.course_id.in_(org_courses), 'task submission'), (CertificateUser, CertificateUser.certification_id.in_(org_certifications), 'certificate')]
    for model, in_org, label in sweeps:
        stale = (await db_session.execute(select(model.id).where(in_org, model.user_id.notin_(demo_user_ids)))).scalars().all()
        if not stale:
            continue
        await db_session.execute(delete(model).where(model.id.in_(stale)))
        stats.drift_deleted += len(stale)
        logger.info('Demo drift: removed %s visitor %s row(s)', len(stale), label)

async def _delete_certification_drift(db_session: AsyncSession, org: Organization, registry: Registry, stats: SyncStats) -> None:
    """Remove certifications a visitor added to a bundle course.

    Also scoped through a parent rather than an ``org_id``: Certifications
    hangs off the course. The sync only ever looks up its own certification by
    registry entry, so a second one added through the UI is invisible to it and
    would survive every refresh, leaving the course offering two certificates
    and the next prospect wondering which is real.
    """
    course_ids = (await db_session.execute(select(Course.id).where(Course.org_id == org.id))).scalars().all()
    if not course_ids:
        return
    owned = registry.uuids_of_kind(DemoEntityKind.CERTIFICATION)
    stale = (await db_session.execute(select(Certifications.id).where(Certifications.course_id.in_(course_ids), Certifications.certification_uuid.notin_(owned) if owned else True))).scalars().all()
    if not stale:
        return
    await db_session.execute(delete(Certifications).where(Certifications.id.in_(stale)))
    stats.drift_deleted += len(stale)
    logger.info('Demo drift: removed %s certification(s)', len(stale))

async def _delete_comment_drift(db_session: AsyncSession, org: Organization, registry: Registry, stats: SyncStats) -> None:
    """Remove comments a visitor left on the bundle's discussions.

    Handled apart from the sweep above because DiscussionComment has no
    ``org_id``: it reaches the organization through its discussion, so the
    generic query cannot scope it. A comment on a discussion the visitor
    *created* goes away with that discussion; this is the other case, where the
    discussion is bundle-owned and survives.

    Left out, whatever the last visitor typed stays under a seeded discussion
    for every prospect who follows.
    """
    discussion_ids = (await db_session.execute(select(Discussion.id).join(Community, Community.id == Discussion.community_id).where(Community.org_id == org.id))).scalars().all()
    if not discussion_ids:
        return
    owned = registry.uuids_of_kind(DemoEntityKind.DISCUSSION_COMMENT)
    stale = (await db_session.execute(select(DiscussionComment.id).where(DiscussionComment.discussion_id.in_(discussion_ids), DiscussionComment.comment_uuid.notin_(owned) if owned else True))).scalars().all()
    if not stale:
        return
    await db_session.execute(delete(DiscussionComment).where(DiscussionComment.id.in_(stale)))
    stats.drift_deleted += len(stale)
    logger.info('Demo drift: removed %s discussion comment(s)', len(stale))

async def _sync_progress(db_session: AsyncSession, bundle: Bundle, org: Organization, registry: Registry, epoch: str, students: list[StudentPlan], users: dict[str, User], stats: SyncStats) -> None:
    """Reconcile trails, submissions, certificates and activity days.

    Every value here is a function of (student, epoch), so on a settled demo
    the desired state already matches what is stored and nothing is written.
    """
    stats.steps.append('progress')
    course_rows = {course.slug: registry.get(DemoEntityKind.COURSE, course.slug) for course in bundle.courses}
    course_ids = {key: entry.entity_id for key, entry in course_rows.items() if entry is not None}
    activities_by_course: dict[str, list[int]] = {}
    for course_key, course_id in course_ids.items():
        rows = (await db_session.execute(select(ChapterActivity.activity_id).join(Activity, Activity.id == ChapterActivity.activity_id).join(CourseChapter, CourseChapter.chapter_id == ChapterActivity.chapter_id).where(ChapterActivity.course_id == course_id, Activity.published.is_(True)).order_by(CourseChapter.order, ChapterActivity.order))).scalars().all()
        activities_by_course[course_key] = list(rows)
    for plan in students:
        user = users.get(plan.bundle_key)
        if user is None:
            continue
        trail = (await db_session.execute(select(Trail).where(Trail.user_id == user.id, Trail.org_id == org.id))).scalars().first()
        if trail is None:
            trail = Trail(trail_uuid=_prefixed('trail'), org_id=org.id, user_id=user.id, creation_date=_days_ago(epoch, HISTORY_DAYS), update_date=_now())
            db_session.add(trail)
            await db_session.flush()
            stats.created += 1
        for course_key in plan.course_keys:
            course_id = course_ids.get(course_key)
            if course_id is None:
                continue
            await _sync_trail_run(db_session, plan, epoch, trail, user, org, course_key, course_id, activities_by_course.get(course_key, []), stats)
        await _sync_activity_days(db_session, plan, epoch, user, org, stats)
    await _sync_submissions(db_session, bundle, org, registry, epoch, students, users, stats)
    await _sync_certificates(db_session, bundle, org, registry, epoch, students, users, activities_by_course, stats)
    await _delete_progress_drift(db_session, org, students, users, course_ids, stats)

async def _delete_progress_drift(db_session: AsyncSession, org: Organization, students: list[StudentPlan], users: dict[str, User], course_ids: dict[str, int], stats: SyncStats) -> None:
    """Remove progress for courses a learner is no longer enrolled in.

    The daily epoch roll re-draws every persona, so a learner's enrolled course
    set changes. The per-learner passes above only ever visit courses in the
    CURRENT plan, so anything that fell out was left behind, and because it is
    only ever added to, enrolments accumulate until all forty learners appear
    enrolled in the entire catalogue with frozen progress bars, stale rows in
    the grading inbox and certificates their plan says they never earned.

    That is the persona design (lurkers take one course, completers take six)
    quietly erasing itself over a fortnight of uptime. Reconciling here restores
    the module's stated invariant: the demo is a function of (bundle, epoch),
    not of how long it has been running.

    TrailStep cascades from TrailRun, so deleting the run takes its steps.
    """
    wanted_runs: set[tuple[int, int]] = set()
    for plan in students:
        user = users.get(plan.bundle_key)
        if user is None:
            continue
        for course_key in plan.course_keys:
            course_id = course_ids.get(course_key)
            if course_id is not None:
                wanted_runs.add((int(user.id), int(course_id)))
    demo_user_ids = {int(u.id) for u in users.values() if u.id is not None}
    if not demo_user_ids:
        return
    existing_runs = (await db_session.execute(select(TrailRun.id, TrailRun.user_id, TrailRun.course_id).where(TrailRun.org_id == org.id, TrailRun.user_id.in_(demo_user_ids)))).all()
    stale_run_ids = [row.id for row in existing_runs if (int(row.user_id), int(row.course_id)) not in wanted_runs]
    if stale_run_ids:
        await db_session.execute(delete(TrailRun).where(TrailRun.id.in_(stale_run_ids)))
        stats.drift_deleted += len(stale_run_ids)
    stale_course_ids_by_user: dict[int, set[int]] = {}
    for row in existing_runs:
        key = (int(row.user_id), int(row.course_id))
        if key not in wanted_runs:
            stale_course_ids_by_user.setdefault(int(row.user_id), set()).add(int(row.course_id))
    for user_id, stale_courses in stale_course_ids_by_user.items():
        stale_assignments = (await db_session.execute(select(Assignment.id).where(Assignment.course_id.in_(stale_courses)))).scalars().all()
        if not stale_assignments:
            continue
        await db_session.execute(delete(AssignmentUserSubmission).where(AssignmentUserSubmission.user_id == user_id, AssignmentUserSubmission.assignment_id.in_(stale_assignments)))
        await db_session.execute(delete(AssignmentTaskSubmission).where(AssignmentTaskSubmission.user_id == user_id, AssignmentTaskSubmission.course_id.in_(stale_courses)))
        stale_certifications = (await db_session.execute(select(Certifications.id).where(Certifications.course_id.in_(stale_courses)))).scalars().all()
        if stale_certifications:
            await db_session.execute(delete(CertificateUser).where(CertificateUser.user_id == user_id, CertificateUser.certification_id.in_(stale_certifications)))

async def _sync_trail_run(db_session: AsyncSession, plan: StudentPlan, epoch: str, trail: Trail, user: User, org: Organization, course_key: str, course_id: int, activity_ids: list[int], stats: SyncStats) -> None:
    fraction = plan.completion.get(course_key, 0.0)
    completed = completed_activity_count(len(activity_ids), fraction)
    if fraction >= 1.0 and activity_ids:
        status = 'STATUS_COMPLETED'
    elif plan.persona.run_status:
        status = plan.persona.run_status
    else:
        status = 'STATUS_IN_PROGRESS'
    run = (await db_session.execute(select(TrailRun).where(TrailRun.trail_id == trail.id, TrailRun.course_id == course_id, TrailRun.user_id == user.id))).scalars().first()
    if run is None:
        run = TrailRun(trailrun_uuid=_prefixed('trailrun'), trail_id=trail.id, course_id=course_id, org_id=org.id, user_id=user.id, status=status, creation_date=step_timestamp(epoch, plan, course_key, 0), update_date=_now())
        db_session.add(run)
        await db_session.flush()
        stats.created += 1
    elif _apply(run, status=status):
        run.update_date = _now()
        db_session.add(run)
        stats.updated += 1
    wanted_ids = activity_ids[:completed]
    existing = (await db_session.execute(select(TrailStep).where(TrailStep.trailrun_id == run.id, TrailStep.user_id == user.id))).scalars().all()
    existing_by_activity = {row.activity_id: row for row in existing}
    for position, activity_id in enumerate(wanted_ids):
        row = existing_by_activity.pop(activity_id, None)
        if row is None:
            db_session.add(TrailStep(complete=True, teacher_verified=False, grade='', trailrun_id=run.id, trail_id=trail.id, activity_id=activity_id, course_id=course_id, org_id=org.id, user_id=user.id, creation_date=step_timestamp(epoch, plan, course_key, position), update_date=step_timestamp(epoch, plan, course_key, position)))
            stats.created += 1
        elif _apply(row, complete=True):
            db_session.add(row)
            stats.updated += 1
    if existing_by_activity:
        await db_session.execute(delete(TrailStep).where(TrailStep.id.in_([row.id for row in existing_by_activity.values()])))
        stats.drift_deleted += len(existing_by_activity)

async def _sync_activity_days(db_session: AsyncSession, plan: StudentPlan, epoch: str, user: User, org: Organization, stats: SyncStats) -> None:
    wanted = set(activity_days(epoch, plan))
    existing = (await db_session.execute(select(UserActivityDay).where(UserActivityDay.org_id == org.id, UserActivityDay.user_id == user.id))).scalars().all()
    existing_days = {row.activity_date for row in existing}
    for day in wanted - existing_days:
        db_session.add(UserActivityDay(org_id=org.id, user_id=user.id, activity_date=day))
        stats.created += 1
    stale = [row for row in existing if row.activity_date not in wanted]
    if stale:
        await db_session.execute(delete(UserActivityDay).where(UserActivityDay.id.in_([row.id for row in stale])))
        stats.drift_deleted += len(stale)

async def _sync_submissions(db_session: AsyncSession, bundle: Bundle, org: Organization, registry: Registry, epoch: str, students: list[StudentPlan], users: dict[str, User], stats: SyncStats) -> None:
    for course_key, sidecar in bundle.assignments.items():
        assignment_entry = registry.get(DemoEntityKind.ASSIGNMENT, course_key)
        if assignment_entry is None:
            continue
        assignment = (await db_session.execute(select(Assignment).where(Assignment.id == assignment_entry.entity_id))).scalars().first()
        if assignment is None:
            continue
        tasks = []
        for task_spec in sidecar['tasks']:
            entry = registry.get(DemoEntityKind.ASSIGNMENT_TASK, task_spec['slug'])
            if entry is None:
                continue
            task = (await db_session.execute(select(AssignmentTask).where(AssignmentTask.id == entry.entity_id))).scalars().first()
            if task is not None:
                tasks.append((task_spec, task))
        for plan in students:
            if course_key not in plan.course_keys:
                continue
            user = users.get(plan.bundle_key)
            if user is None:
                continue
            status = submission_status_for(plan, epoch, course_key)
            if status == 'NOT_SUBMITTED':
                continue
            template = submission_template_for(plan, epoch, course_key)
            total = 0
            for task_spec, task in tasks:
                grade = grade_for(plan, epoch, course_key, task_spec['max_grade_value'])
                if status != 'GRADED':
                    grade = 0
                total += grade
                await _sync_task_submission(db_session, plan, epoch, course_key, user, org, task, task_spec, template, grade, status, stats)
            await _sync_user_submission(db_session, plan, epoch, course_key, user, org, assignment, status, total, stats)

async def _sync_task_submission(db_session: AsyncSession, plan: StudentPlan, epoch: str, course_key: str, user: User, org: Organization, task: AssignmentTask, task_spec: dict, template: str, grade: int, status: str, stats: SyncStats) -> None:
    payload = task_spec['submissions'][template]
    row = (await db_session.execute(select(AssignmentTaskSubmission).where(AssignmentTaskSubmission.user_id == user.id, AssignmentTaskSubmission.assignment_task_id == task.id))).scalars().first()
    fields = dict(task_submission=payload, grade=grade, task_submission_grade_feedback='', manually_graded=False)
    if row is None:
        db_session.add(AssignmentTaskSubmission(assignment_task_submission_uuid=_prefixed('assignmenttasksubmission'), assignment_type=task.assignment_type, user_id=user.id, assignment_task_id=task.id, course_id=task.course_id, chapter_id=task.chapter_id, activity_id=task.activity_id, creation_date=step_timestamp(epoch, plan, course_key, 0), update_date=_now(), **fields))
        stats.created += 1
    elif _apply(row, **fields):
        row.update_date = _now()
        db_session.add(row)
        stats.updated += 1

async def _sync_user_submission(db_session: AsyncSession, plan: StudentPlan, epoch: str, course_key: str, user: User, org: Organization, assignment: Assignment, status: str, total: int, stats: SyncStats) -> None:
    row = (await db_session.execute(select(AssignmentUserSubmission).where(AssignmentUserSubmission.user_id == user.id, AssignmentUserSubmission.assignment_id == assignment.id))).scalars().first()
    fields = dict(submission_status=status, grade=total, overall_feedback='')
    if row is None:
        db_session.add(AssignmentUserSubmission(assignmentusersubmission_uuid=_prefixed('assignmentusersubmission'), user_id=user.id, assignment_id=assignment.id, attempt_number=1, creation_date=step_timestamp(epoch, plan, course_key, 1), update_date=_now(), **fields))
        stats.created += 1
    elif _apply(row, **fields):
        row.update_date = _now()
        db_session.add(row)
        stats.updated += 1

async def _sync_certificates(db_session: AsyncSession, bundle: Bundle, org: Organization, registry: Registry, epoch: str, students: list[StudentPlan], users: dict[str, User], activities_by_course: dict[str, list[int]], stats: SyncStats) -> None:
    """Issue a certificate to learners who finished a certified course.

    Mirrors the eligibility rule the application uses (all published activities
    complete) rather than calling the real issuing function, which would fire
    webhooks and analytics for every one of these.
    """
    for course_spec in bundle.courses:
        if not course_spec.certification:
            continue
        entry = registry.get(DemoEntityKind.CERTIFICATION, course_spec.slug)
        if entry is None:
            continue
        for plan in students:
            user = users.get(plan.bundle_key)
            if user is None or course_spec.slug not in plan.course_keys:
                continue
            total = len(activities_by_course.get(course_spec.slug, []))
            done = completed_activity_count(total, plan.completion.get(course_spec.slug, 0.0))
            eligible = total > 0 and done >= total
            row = (await db_session.execute(select(CertificateUser).where(CertificateUser.user_id == user.id, CertificateUser.certification_id == entry.entity_id))).scalars().first()
            if eligible and row is None:
                db_session.add(CertificateUser(user_certification_uuid=_certificate_code(user.user_uuid, epoch), user_id=user.id, certification_id=entry.entity_id, created_at=_days_ago(epoch, 2 + plan.index % 21, hour=9 + plan.index % 8), updated_at=_now()))
                stats.created += 1
            elif not eligible and row is not None:
                await db_session.execute(delete(CertificateUser).where(CertificateUser.id == row.id))
                stats.drift_deleted += 1

def _certificate_code(user_uuid: str, epoch: str) -> str:
    """Readable certificate reference, in the format the app already uses."""
    stamp = epoch.replace('-', '')
    tail = user_uuid[-4:] if len(user_uuid) >= 4 else user_uuid
    return f'RA-{stamp}-{tail}-{uuid4().hex[:8]}'

async def _assert_single_tenant(db_session: AsyncSession, org: Organization) -> None:
    """Confirm nothing was written outside the demo organization.

    A sync with an org_id bug is a cross-tenant data-injection bug, so it is
    worth two cheap counts to be certain rather than confident.
    """
    demo_entity_leak = (await db_session.execute(select(func.count()).select_from(DemoEntity).where(DemoEntity.org_id != org.id))).scalar_one()
    if demo_entity_leak:
        raise RuntimeError(f'demo sync registered {demo_entity_leak} row(s) outside the demo org')

async def _finalize(db_session: AsyncSession, state: DemoState, org: Organization, bundle: Bundle, epoch: str, stats: SyncStats) -> None:
    state.org_id = org.id
    state.state = DemoStateEnum.READY
    state.bundle_version = bundle.manifest.bundle_version
    state.content_epoch = epoch
    state.last_refresh_at = _now()
    state.last_step = stats.steps[-1] if stats.steps else ''
    state.last_error = None
    state.stats = stats.as_dict()
    state.update_date = _now()
    if stats.provisioned:
        state.last_provision_at = _now()
    db_session.add(state)
    try:
        pass
    except Exception:
        logger.debug('Could not invalidate usage cache for the demo org')
    try:
        from src.services.orgs.cache import invalidate_org_cache
        invalidate_org_cache(org.slug)
    except Exception:
        logger.debug('Could not invalidate org cache for the demo org')
