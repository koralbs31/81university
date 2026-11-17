"""Remove the retired subscription and purchased-pack metadata."""
from alembic import op
import sqlalchemy as sa

revision = 'd04f2c8e3b71'
down_revision = 'c93e1b7d2a60'
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ('orgpack', 'organization_plan_history'):
        op.execute(sa.text(f'DROP TABLE IF EXISTS "{table}" CASCADE'))
    op.execute(sa.text("DELETE FROM resourceauthor WHERE resource_uuid ~ '^(podcast|episode|board|playground|folder|media)_'"))
    op.execute(sa.text("DELETE FROM usergroupresource WHERE resource_uuid ~ '^(podcast|episode|board|playground|folder|media)_'"))


def downgrade() -> None:
    raise RuntimeError('Retired billing metadata must be restored from a database backup.')
