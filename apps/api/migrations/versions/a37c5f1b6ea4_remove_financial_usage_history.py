"""Remove financial accounting and retired webhook subscriptions."""
import json
from alembic import op
import sqlalchemy as sa

revision = 'a37c5f1b6ea4'
down_revision = 'f26b4e0a5d93'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if 'usageevent' in tables:
        op.drop_table('usageevent')
    if 'webhook_endpoint' in tables:
        retired = {'pack_activated', 'pack_deactivated', 'payment_access_granted', 'payment_access_revoked'}
        for row in bind.execute(sa.text('SELECT id, events FROM webhook_endpoint')).mappings():
            events = row['events'] or []
            if isinstance(events, str):
                events = json.loads(events)
            retained = [event for event in events if event not in retired]
            if retained != events:
                bind.execute(sa.text('UPDATE webhook_endpoint SET events = :events WHERE id = :id').bindparams(sa.bindparam('events', type_=sa.JSON())), {'id': row['id'], 'events': retained})


def downgrade():
    raise RuntimeError('Financial data was removed; restore a backup to roll back.')
