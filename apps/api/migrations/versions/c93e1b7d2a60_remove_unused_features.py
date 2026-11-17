"""Remove library, podcasts, boards, playgrounds, announcements and profile extras.

Revision ID: c93e1b7d2a60
Revises: f5a8b3d0c2e4
"""
from alembic import op
import sqlalchemy as sa

revision = 'c93e1b7d2a60'
down_revision = 'f5a8b3d0c2e4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in (
        'boardmember', 'board', 'playgroundreaction', 'playground',
        'podcastepisode', 'podcast', 'foldercontent', 'folder',
        'mediasharetoken', 'media', 'courseupdate',
    ):
        op.execute(sa.text(f'DROP TABLE IF EXISTS "{table}" CASCADE'))
    for column in ('bio', 'details', 'profile'):
        op.execute(sa.text(f'ALTER TABLE "user" DROP COLUMN IF EXISTS "{column}"'))
    op.execute(sa.text("""
        UPDATE role SET rights = (rights::jsonb - ARRAY[
            'podcasts','boards','playgrounds','folders','media','payments'
        ])::json
    """))
    # Old organization settings must not resurrect removed menu entries or flags.
    connection = op.get_bind()
    rows = connection.execute(sa.text('SELECT id, config FROM organizationconfig')).mappings().all()
    removed = {'podcast','podcasts','boards','board','playground','playgrounds',
               'library','folders','calendar','payments','purchases','store','collaboration'}

    def clean(value):
        if isinstance(value, dict):
            return {key: clean(item) for key, item in value.items() if key not in removed}
        if isinstance(value, list):
            return [clean(item) for item in value if not (
                isinstance(item, dict) and item.get('type') in removed
            )]
        return value

    import json
    for row in rows:
        config = clean(row['config'] or {})
        connection.execute(sa.text('UPDATE organizationconfig SET config=CAST(:config AS json) WHERE id=:id'),
                           {'id': row['id'], 'config': json.dumps(config)})


def downgrade() -> None:
    raise RuntimeError('Removed feature data must be restored from a database backup.')
