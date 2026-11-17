"""Edge branches of the demo sync, driven through its phase helpers.

test_demo_sync.py and test_demo_drift.py run the whole sync against the real
bundle, which only ever takes the paths that bundle happens to need. The
branches here are the ones a visitor, a bundle edit or an Enterprise build
reaches: rows tampered with between refreshes, bundle entries pointing at
things that are gone, and the storefront. Each phase is called directly with a
tiny hand-written bundle, so none of these pay for a full build.
"""
import enum
import sys
import types
from datetime import datetime
from types import SimpleNamespace
import pytest
from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, select
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from src.db.courses.activities import Activity, ActivityTypeEnum
from src.db.courses.chapter_activities import ChapterActivity
from src.db.courses.chapters import Chapter
from src.db.courses.course_chapters import CourseChapter
from src.db.demo_entities import DemoEntity, DemoEntityKind
from src.db.demo_state import DEMO_STATE_ID, DemoState, DemoStateEnum
from src.db.organization_config import OrganizationConfig
from src.db.trails import Trail
from src.services.demo import sync as sync_module
from src.services.demo.bundle_loader import ActivitySpec, ChapterSpec, CourseSpec, PersonaSpec, get_bundle
from src.services.demo.progress import StudentPlan
from src.services.demo.registry import Registry
from src.services.demo.sync import SyncStats
EPOCH = '2026-08-09'
VERSION = 'test-1'

@pytest.fixture
def no_uploads(monkeypatch):
    """Skip real file writes, returning the name the real helper would."""

    async def _fake_upload(**kwargs):
        return sync_module._upload_name(uuid=kwargs['uuid'], directory=kwargs['directory'], name=kwargs['name'], version=kwargs.get('version', ''))
    monkeypatch.setattr(sync_module, '_upload', _fake_upload)

def _stats() -> SyncStats:
    return SyncStats(epoch=EPOCH)
_PERSONA = PersonaSpec(id='steady', weight=1, min_courses=1, max_courses=1, min_completion=1.0, max_completion=1.0, assignments='pending')

def _plan(index: int, *, cohort='co-one', course_keys=(), completion=None) -> StudentPlan:
    return StudentPlan(index=index, bundle_key=f'student-{index}', first_name=f'First{index}', last_name='Learner', email=f'student{index}@demo.example.com', username=f'demo-student-{index}', avatar=None, persona=_PERSONA, cohort_key=cohort, course_keys=list(course_keys), completion=dict(completion or {}), joined_days_ago=30)

def _course_spec(slug='c-one', *, chapters=None, certification=False) -> CourseSpec:
    return CourseSpec(slug=slug, name=f'Course {slug}', description='A course', thumbnail='thumb.webp', section='s-one', certification=certification, chapters=chapters if chapters is not None else [ChapterSpec(slug='ch-one', name='Chapter one', activities=[ActivitySpec(slug='a-doc', name='Doc'), ActivitySpec(slug='a-quiz', name='Quiz', kind='assignment'), ActivitySpec(slug='a-orphan', name='Orphan')])])

async def _students(db, org, registry, plans):
    return await sync_module._sync_students(db, None, org, registry, plans, _stats())

def _task(slug: str) -> dict:
    answer = {'submissions': []}
    return {'slug': slug, 'title': f'Task {slug}', 'type': 'QUIZ', 'contents': {'questions': []}, 'max_grade_value': 50, 'submissions': {'correct': answer, 'partial': answer, 'incorrect': answer}}

def _sidecar(activity_key: str, tasks=None) -> dict:
    return {'activity_key': activity_key, 'title': 'Exercise', 'description': 'Do the thing', 'tasks': tasks if tasks is not None else [_task('t-one')]}

async def test_a_failure_does_not_mark_a_demo_another_run_made_ready(db, caplog):
    """The first-provision race: a READY row now means somebody else won.

    With no state row at the start (started_from None), a READY row after the
    rollback is another replica's success, so the error is recorded but the
    demo is not flipped to FAILED and hidden.
    """
    db.add(DemoState(id=DEMO_STATE_ID, state=DemoStateEnum.READY, update_date='earlier'))
    await db.commit()
    with caplog.at_level('WARNING', logger=sync_module.logger.name):
        await sync_module._record_sync_failure(db, ValueError('boom'), started_from=None)
    state = (await db.execute(select(DemoState))).scalars().one()
    assert state.state == DemoStateEnum.READY
    assert state.last_error == 'ValueError: boom'
    assert 'another run has since succeeded' in caplog.text

async def test_a_tampered_org_config_is_put_back(db):
    """Every visitor is an admin, so the plan config is theirs to change."""
    bundle = get_bundle()
    org, created = await sync_module._sync_org(db, bundle, _stats())
    assert created is True
    config = (await db.execute(select(OrganizationConfig).where(OrganizationConfig.org_id == org.id))).scalars().one()
    config.config = {'plan': 'free'}
    db.add(config)
    await db.flush()
    stats = _stats()
    _, created = await sync_module._sync_org(db, bundle, stats)
    assert created is False
    assert stats.updated == 1
    assert config.config == sync_module._demo_org_config()

async def test_no_byline_is_written_before_any_student_exists(db, org):
    """The author is a seeded student; with none yet, nobody is attributed."""
    from src.db.resource_authors import ResourceAuthor
    stats = _stats()
    await sync_module._sync_resource_author_for(db, 'course_x', org, stats)
    assert (await db.execute(select(ResourceAuthor))).scalars().all() == []
    assert stats.created == 0

async def test_reordered_chapters_and_activities_follow_the_bundle(db, org, course):
    registry = Registry(org.id, [])

    def spec(chapter_order, activity_order):
        chapters = {'ch-a': ChapterSpec(slug='ch-a', name='A', activities=[{'x': ActivitySpec(slug='act-x', name='X'), 'y': ActivitySpec(slug='act-y', name='Y')}[key] for key in activity_order]), 'ch-b': ChapterSpec(slug='ch-b', name='B', activities=[])}
        return SimpleNamespace(chapters=[chapters[key] for key in chapter_order])
    await sync_module._sync_chapters(db, spec(['ch-a', 'ch-b'], ['x', 'y']), course, org, registry, EPOCH, _stats())
    await db.flush()
    stats = _stats()
    await sync_module._sync_chapters(db, spec(['ch-b', 'ch-a'], ['y', 'x']), course, org, registry, EPOCH, stats)
    await db.flush()
    chapter_order = dict((await db.execute(select(Chapter.name, CourseChapter.order).join(CourseChapter, CourseChapter.chapter_id == Chapter.id))).all())
    activity_order = dict((await db.execute(select(Activity.name, ChapterActivity.order).join(ChapterActivity, ChapterActivity.activity_id == Activity.id))).all())
    assert chapter_order == {'A': 1, 'B': 0}
    assert activity_order == {'Y': 0, 'X': 1}
    assert stats.updated == 4

async def test_a_video_activity_stores_only_its_youtube_id(db, org, course, chapter):
    chapter_spec = SimpleNamespace(activities=[ActivitySpec(slug='a-vid', name='Video', kind='video', youtube_id='abc123')])
    await sync_module._sync_activities(db, chapter_spec, chapter, course, org, Registry(org.id, []), EPOCH, _stats())
    activity = (await db.execute(select(Activity).where(Activity.name == 'Video'))).scalars().one()
    assert activity.content == {'youtube_id': 'abc123'}
    assert activity.activity_type == ActivityTypeEnum.TYPE_VIDEO

class _EEBase(DeclarativeBase):
    """Own metadata, so these tables never join the shared test schema."""

class PaymentProviderEnum(str, enum.Enum):
    STRIPE = 'stripe'
    CUSTOM = 'custom'

class OfferTypeEnum(str, enum.Enum):
    ONE_TIME = 'one_time'
    SUBSCRIPTION = 'subscription'

class OfferPriceTypeEnum(str, enum.Enum):
    FIXED_PRICE = 'fixed_price'
    CUSTOMER_CHOICE = 'customer_choice'

class EnrollmentStatusEnum(str, enum.Enum):
    ACTIVE = 'active'
    PENDING = 'pending'

class PaymentsConfig(_EEBase):
    __tablename__ = 'fake_payments_config'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean)
    active: Mapped[bool] = mapped_column(Boolean)
    provider: Mapped[PaymentProviderEnum] = mapped_column(SAEnum(PaymentProviderEnum))
    provider_config: Mapped[dict] = mapped_column(JSON)
    creation_date: Mapped[datetime] = mapped_column(DateTime)
    update_date: Mapped[datetime] = mapped_column(DateTime)

class PaymentsGroup(_EEBase):
    __tablename__ = 'fake_payments_group'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String)
    description: Mapped[str] = mapped_column(String)
    creation_date: Mapped[datetime] = mapped_column(DateTime)
    update_date: Mapped[datetime] = mapped_column(DateTime)

class PaymentsGroupResource(_EEBase):
    __tablename__ = 'fake_payments_group_resource'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    payments_group_id: Mapped[int] = mapped_column(Integer)
    resource_uuid: Mapped[str] = mapped_column(String)
    org_id: Mapped[int] = mapped_column(Integer)
    creation_date: Mapped[datetime] = mapped_column(DateTime)
    update_date: Mapped[datetime] = mapped_column(DateTime)

class PaymentsOffer(_EEBase):
    __tablename__ = 'fake_payments_offer'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(Integer)
    payments_config_id: Mapped[int] = mapped_column(Integer)
    offer_uuid: Mapped[str] = mapped_column(String)
    provider_product_id: Mapped[str] = mapped_column(String)
    name: Mapped[str] = mapped_column(String)
    description: Mapped[str] = mapped_column(String)
    offer_type: Mapped[OfferTypeEnum] = mapped_column(SAEnum(OfferTypeEnum))
    price_type: Mapped[OfferPriceTypeEnum] = mapped_column(SAEnum(OfferPriceTypeEnum))
    amount: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String)
    benefits: Mapped[str] = mapped_column(String)
    is_publicly_listed: Mapped[bool] = mapped_column(Boolean)
    payments_group_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    external_checkout_url: Mapped[str | None] = mapped_column(String, nullable=True)
    creation_date: Mapped[datetime] = mapped_column(DateTime)
    update_date: Mapped[datetime] = mapped_column(DateTime)

class PaymentsOfferResource(_EEBase):
    __tablename__ = 'fake_payments_offer_resource'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    offer_id: Mapped[int] = mapped_column(Integer)
    resource_uuid: Mapped[str] = mapped_column(String)
    org_id: Mapped[int] = mapped_column(Integer)
    creation_date: Mapped[datetime] = mapped_column(DateTime)
    update_date: Mapped[datetime] = mapped_column(DateTime)

class PaymentsEnrollment(_EEBase):
    __tablename__ = 'fake_payments_enrollment'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    enrollment_uuid: Mapped[str] = mapped_column(String)
    offer_id: Mapped[int] = mapped_column(Integer)
    user_id: Mapped[int] = mapped_column(Integer)
    org_id: Mapped[int] = mapped_column(Integer)
    status: Mapped[EnrollmentStatusEnum] = mapped_column(SAEnum(EnrollmentStatusEnum))
    creation_date: Mapped[datetime] = mapped_column(DateTime)
    update_date: Mapped[datetime] = mapped_column(DateTime)

def _module(name: str, **attrs) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module

@pytest.fixture
async def fake_ee(engine, monkeypatch):
    """A complete stand-in Enterprise payments package, with its tables."""
    async with engine.begin() as conn:
        await conn.run_sync(_EEBase.metadata.create_all)
    for name, module in (('ee', _module('ee')), ('ee.db', _module('ee.db')), ('ee.db.payments', _module('ee.db.payments')), ('ee.db.payments.payments', _module('ee.db.payments.payments', PaymentProviderEnum=PaymentProviderEnum, PaymentsConfig=PaymentsConfig)), ('ee.db.payments.payments_enrollments', _module('ee.db.payments.payments_enrollments', EnrollmentStatusEnum=EnrollmentStatusEnum, PaymentsEnrollment=PaymentsEnrollment)), ('ee.db.payments.payments_groups', _module('ee.db.payments.payments_groups', PaymentsGroup=PaymentsGroup, PaymentsGroupResource=PaymentsGroupResource, PaymentsOfferResource=PaymentsOfferResource)), ('ee.db.payments.payments_offers', _module('ee.db.payments.payments_offers', OfferPriceTypeEnum=OfferPriceTypeEnum, OfferTypeEnum=OfferTypeEnum, PaymentsOffer=PaymentsOffer))):
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr('src.core.deployment_mode.get_deployment_mode', lambda: 'saas')

@pytest.fixture
def no_ee(monkeypatch):
    """A build that claims a storefront but cannot import the models."""
    for name in list(sys.modules):
        if name == 'ee' or name.startswith('ee.'):
            monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setitem(sys.modules, 'ee', None)
    monkeypatch.setattr(sync_module, '_store_unsupported_reason', lambda: None)

async def _all(db, model):
    return (await db.execute(select(model))).scalars().all()

async def test_visitor_progress_is_left_alone_before_any_student_exists(db, org, admin_user):
    """With no students to compare against, every row would look like a visitor's."""
    trail = Trail(org_id=org.id, user_id=admin_user.id, trail_uuid='trail_visitor')
    db.add(trail)
    await db.flush()
    stats = _stats()
    await sync_module._delete_visitor_progress_drift(db, org, Registry(org.id, []), stats)
    assert await _all(db, Trail) == [trail]
    assert stats.drift_deleted == 0

async def test_drift_passes_with_nothing_to_scope_return_quietly(db, org):
    """No courses means no certifications; no discussions means no comments."""
    stats = _stats()
    registry = Registry(org.id, [])
    await sync_module._delete_certification_drift(db, org, registry, stats)
    await sync_module._delete_comment_drift(db, org, registry, stats)
    await sync_module._delete_progress_drift(db, org, [], {}, {}, stats)
    assert stats.drift_deleted == 0

async def test_a_registry_row_outside_the_demo_org_aborts_the_sync(db, org, other_org):
    db.add(DemoEntity(kind=DemoEntityKind.COURSE, bundle_key='c-leak', entity_id=1, entity_uuid='course_leak', org_id=other_org.id))
    await db.flush()
    with pytest.raises(RuntimeError, match='outside the demo org'):
        await sync_module._assert_single_tenant(db, org)
