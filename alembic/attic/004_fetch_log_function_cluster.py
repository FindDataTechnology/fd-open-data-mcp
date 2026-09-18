"""add fetch_log.function_id + cluster_id (fix-silent-zero-yield-crawls)

The table recorded source/real_source only — nothing identifying the endpoint
actually called or the egress it was called from. One source spans reachable
and unreachable hosts simultaneously, so failure attribution and per-(cluster,
function) demotion both need these keys.

Revision ID: 004_fetch_log_function_cluster
Revises: 003_function_bulk_history
Create Date: 2026-08-27
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '004_fetch_log_function_cluster'
down_revision = '003_function_bulk_history'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('fetch_log', sa.Column('function_id', sa.Integer(),
                                        sa.ForeignKey('functions.id'),
                                        nullable=True))
    op.create_index('ix_fetch_log_function_id', 'fetch_log', ['function_id'])
    op.add_column('fetch_log', sa.Column('cluster_id', sa.Integer(), nullable=True))
    op.create_index('ix_fetch_log_cluster_id', 'fetch_log', ['cluster_id'])


def downgrade() -> None:
    op.drop_index('ix_fetch_log_cluster_id', table_name='fetch_log')
    op.drop_column('fetch_log', 'cluster_id')
    op.drop_index('ix_fetch_log_function_id', table_name='fetch_log')
    op.drop_column('fetch_log', 'function_id')
