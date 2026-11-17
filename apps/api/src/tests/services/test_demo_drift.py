"""What a visitor leaves behind, and what the next refresh does about it.

Every visitor to the demo is an admin of it, so this is the surface that keeps
one prospect's mess (and one prospect's identity) out of the next prospect's
session. The passes here are the ones the generic uuid sweep cannot reach:
their tables hang off a parent rather than carrying an org_id.
"""
from datetime import date, datetime
import pytest
from sqlalchemy import func, select
from src.db.organizations import Organization
from src.db.user_organizations import UserOrganization
from src.db.users import User
from src.security.rbac.constants import ADMIN_ROLE_ID
from src.services.demo.sync import sync_demo
from src.tests.conftest import build_once
EPOCH_DAY = date(2026, 8, 9)

@pytest.fixture
def no_uploads(monkeypatch):
    from src.services.demo import sync as sync_module

    async def _fake_upload(**kwargs):
        return sync_module._upload_name(uuid=kwargs['uuid'], directory=kwargs['directory'], name=kwargs['name'], version=kwargs.get('version', ''))
    monkeypatch.setattr(sync_module, '_upload', _fake_upload)

@pytest.fixture
async def synced(engine, db, no_uploads):
    await build_once(engine, db, 'demo_sync', lambda session: sync_demo(session, today=EPOCH_DAY))

@pytest.fixture
async def demo_org(db, synced):
    return (await db.execute(select(Organization).where(Organization.is_demo.is_(True)))).scalars().first()

@pytest.fixture
async def visitor(db, demo_org):
    """A real person who opened the demo, and is therefore an admin of it."""
    now = str(datetime.now())
    user = User(id=909, username='prospect', first_name='Pro', last_name='Spect', email='prospect@example.com', password='hashed', user_uuid='user_prospect909', signup_method='email', creation_date=now, update_date=now)
    db.add(user)
    await db.flush()
    db.add(UserOrganization(user_id=user.id, org_id=demo_org.id, role_id=ADMIN_ROLE_ID, creation_date=now, update_date=now))
    await db.commit()
    return user

async def test_a_custom_role_is_removed_and_its_holders_reassigned(db, demo_org, visitor):
    """role_id has no ON DELETE, so the memberships have to move first.

    Otherwise the DELETE raises and takes the whole sync transaction with it,
    and would keep doing so on every refresh afterwards.
    """
    from src.db.roles import Role, RoleTypeEnum
    role = Role(id=501, name="Visitor's role", org_id=demo_org.id, role_type=RoleTypeEnum.TYPE_ORGANIZATION, role_uuid='role_visitor_made', rights={}, creation_date='2026-08-09', update_date='2026-08-09')
    db.add(role)
    await db.flush()
    membership = (await db.execute(select(UserOrganization).where(UserOrganization.user_id == visitor.id, UserOrganization.org_id == demo_org.id))).scalars().first()
    membership.role_id = role.id
    db.add(membership)
    await db.commit()
    await sync_demo(db, today=EPOCH_DAY)
    assert (await db.execute(select(Role).where(Role.role_uuid == 'role_visitor_made'))).scalars().first() is None
    await db.refresh(membership)
    assert membership.role_id == ADMIN_ROLE_ID, 'the visitor should keep admin'

async def test_a_role_still_held_outside_the_demo_is_left_alone(db, demo_org, visitor, other_org):
    """Deleting it would abort the sync, and reassigning is not ours to do.

    update_user_role resolves a role by uuid with no org scoping, so a demo
    role can end up held by a membership in someone else's organization. The
    sync must never write outside the demo org, so the row is left and logged.
    """
    from src.db.roles import Role, RoleTypeEnum
    role = Role(id=502, name='Escaped role', org_id=demo_org.id, role_type=RoleTypeEnum.TYPE_ORGANIZATION, role_uuid='role_escaped', rights={}, creation_date='2026-08-09', update_date='2026-08-09')
    db.add(role)
    await db.flush()
    db.add(UserOrganization(user_id=visitor.id, org_id=other_org.id, role_id=role.id, creation_date='2026-08-09', update_date='2026-08-09'))
    await db.commit()
    await sync_demo(db, today=EPOCH_DAY)
    survivor = (await db.execute(select(Role).where(Role.role_uuid == 'role_escaped'))).scalars().first()
    assert survivor is not None

async def test_a_visitors_submissions_and_certificate_are_removed(db, demo_org, visitor):
    """Otherwise the next prospect reads it, under a real person's name.

    Everyone here is an admin, so a visitor's submissions show up in the
    grading inbox and their certificate in the certificate list.
    """
    from src.db.courses.assignments import Assignment, AssignmentUserSubmission, AssignmentUserSubmissionStatus
    from src.db.courses.certifications import CertificateUser, Certifications
    from src.db.user_activity import UserActivityDay
    assignment = (await db.execute(select(Assignment).where(Assignment.org_id == demo_org.id).limit(1))).scalars().first()
    certification = (await db.execute(select(Certifications).limit(1))).scalars().first()
    db.add(AssignmentUserSubmission(assignment_id=assignment.id, user_id=visitor.id, assignmentusersubmission_uuid='aus_visitor', submission_status=AssignmentUserSubmissionStatus.SUBMITTED, grade=0, creation_date='2026-08-09', update_date='2026-08-09'))
    db.add(CertificateUser(user_id=visitor.id, certification_id=certification.id, user_certification_uuid='ucert_visitor', creation_date='2026-08-09', update_date='2026-08-09'))
    db.add(UserActivityDay(org_id=demo_org.id, user_id=visitor.id, activity_date=EPOCH_DAY))
    await db.commit()
    await sync_demo(db, today=EPOCH_DAY)
    for model in (AssignmentUserSubmission, CertificateUser, UserActivityDay):
        left = (await db.execute(select(func.count()).select_from(model).where(model.user_id == visitor.id))).scalar_one()
        assert left == 0, f'{model.__name__} survived for a visitor'
    seeded = (await db.execute(select(func.count()).select_from(CertificateUser))).scalar_one()
    assert seeded > 0

def _fake_ee(monkeypatch, *, provider_members, enrolment_attrs):
    """Install a stand-in Enterprise payments package for one test."""
    import sys
    import types
    from enum import Enum
    provider = Enum('PaymentProviderEnum', provider_members, type=str)
    enrolment = type('PaymentsEnrollment', (), dict(enrolment_attrs))
    payments = types.ModuleType('ee.db.payments.payments')
    payments.PaymentProviderEnum = provider
    enrolments = types.ModuleType('ee.db.payments.payments_enrollments')
    enrolments.PaymentsEnrollment = enrolment
    for name, module in (('ee', types.ModuleType('ee')), ('ee.db', types.ModuleType('ee.db')), ('ee.db.payments', types.ModuleType('ee.db.payments')), ('ee.db.payments.payments', payments), ('ee.db.payments.payments_enrollments', enrolments)):
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr('src.core.deployment_mode.get_deployment_mode', lambda: 'saas')
