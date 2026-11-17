"""Remove retired resource activities and unsupported editor nodes."""
import json
from alembic import op
import sqlalchemy as sa
import alembic_postgresql_enum  # noqa: F401 - registers enum operations

revision = 'f26b4e0a5d93'
down_revision = 'e15a3d9f4c82'
branch_labels = None
depends_on = None


def _clean_document(value):
    if isinstance(value, dict):
        if value.get('type') in {'blockMagic', 'blockLibrary'}:
            return None
        result = {key: _clean_document(item) for key, item in value.items()}
        if result.get('type') == 'doc' and not result.get('content'):
            result['content'] = [{'type': 'paragraph'}]
        return result
    if isinstance(value, list):
        return [clean for item in value if (clean := _clean_document(item)) is not None]
    return value


def upgrade():
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if 'activity' in tables:
        # Activity children have cascading foreign keys. UUID link tables do not.
        where = "activity_sub_type::text = 'SUBTYPE_DYNAMIC_RESOURCE'"
        for table in ('resourceauthor', 'usergroupresource'):
            if table in tables:
                op.execute(sa.text(f'DELETE FROM {table} WHERE resource_uuid IN (SELECT activity_uuid FROM activity WHERE {where})'))
        op.execute(sa.text(f'DELETE FROM activity WHERE {where}'))
    for table in ('activity', 'activityversion'):
        if table not in tables:
            continue
        rows = bind.execute(sa.text(f'SELECT id, content FROM {table} WHERE content IS NOT NULL')).mappings()
        for row in rows:
            clean = _clean_document(row['content'])
            if clean is None:
                clean = {'type': 'doc', 'content': [{'type': 'paragraph'}]}
            if clean != row['content']:
                bind.execute(sa.text(f'UPDATE {table} SET content=CAST(:content AS json) WHERE id=:id'),
                             {'id': row['id'], 'content': json.dumps(clean)})
    if 'demoentity' in tables:
        op.execute(sa.text("DELETE FROM demoentity WHERE kind IN ('media','folder','course_update','board','playground','podcast','podcast_episode','payments_group','payments_offer','payments_enrollment')"))
    op.execute(sa.text('DROP TYPE IF EXISTS aigenerationkind'))
    # The removed activity kind must not remain a database enum option.
    values = ['SUBTYPE_DYNAMIC_PAGE', 'SUBTYPE_DYNAMIC_MARKDOWN', 'SUBTYPE_DYNAMIC_EMBED',
              'SUBTYPE_VIDEO_YOUTUBE', 'SUBTYPE_VIDEO_HOSTED', 'SUBTYPE_DOCUMENT_PDF',
              'SUBTYPE_DOCUMENT_DOC', 'SUBTYPE_ASSIGNMENT_ANY', 'SUBTYPE_CUSTOM',
              'SUBTYPE_SCORM_12', 'SUBTYPE_SCORM_2004', 'SUBTYPE_LIVE_SESSION']
    enum = next((e for e in sa.inspect(bind).get_enums() if e['name'] == 'activitysubtypeenum'), None)
    if enum and enum['labels'] != values:
        op.sync_enum_values(enum_schema=enum['schema'], enum_name=enum['name'],
                            new_values=values, affected_columns=[('activity', 'activity_sub_type')])



def downgrade():
    raise RuntimeError('Restore retired content from the pre-removal database backup.')
