"""Remove built-in AI generation, agents, embeddings, and MCP connections."""
from alembic import op
import sqlalchemy as sa

revision = 'e15a3d9f4c82'
down_revision = 'd04f2c8e3b71'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if 'conversation' in tables:
        op.execute(sa.text("DELETE FROM conversation WHERE kind = 'agent'"))
    if 'message' in tables:
        op.execute(sa.text("DELETE FROM message WHERE kind = 'reminder'"))
    # Drop incoming agent references before removing the agent table.
    for table, column in [('conversation', 'agent_id'), ('message', 'sender_agent_id')]:
        if table in tables and column in {c['name'] for c in sa.inspect(bind).get_columns(table)}:
            op.drop_column(table, column)
    for table in ('messagereminder', 'agent', 'aigeneration', 'course_embedding',
                  'mcp_oauth_token', 'mcp_oauth_code', 'mcp_oauth_grant', 'mcp_oauth_client'):
        op.execute(sa.text(f'DROP TABLE IF EXISTS "{table}" CASCADE'))
    if 'organizationconfig' in tables:
        op.execute(sa.text("""
            UPDATE organizationconfig
            SET config = (config::jsonb - 'ai' - 'mcp') ||
                jsonb_build_object(
                    'features', COALESCE(config::jsonb->'features', '{}'::jsonb) - 'ai' - 'mcp',
                    'admin_toggles', COALESCE(config::jsonb->'admin_toggles', '{}'::jsonb) - 'ai' - 'mcp',
                    'overrides', COALESCE(config::jsonb->'overrides', '{}'::jsonb) - 'ai' - 'mcp'
                )
        """))


def downgrade():
    raise RuntimeError('Restore removed AI data from a database backup.')
