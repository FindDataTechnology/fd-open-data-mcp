"""add data_census (add-shard-aware-coverage)

Per-store observation census: the local master table plus each shard foreign
server. Written only by an explicit refresh (panel action or CLI); read by
/panel/data and the data_stats MCP tool.

Revision ID: 008_data_census
Revises: 007_crawl_policy_direct_executor
Create Date: 2026-08-28
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '008_data_census'
down_revision = '007_crawl_policy_direct_executor'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'data_census',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('store', sa.String(64), nullable=False, unique=True, index=True),
        sa.Column('kind', sa.String(16), nullable=False),
        sa.Column('approx_rows', sa.Integer(), nullable=True),
        sa.Column('exact', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('total_size_bytes', sa.Integer(), nullable=True),
        sa.Column('chunks', sa.Integer(), nullable=True),
        sa.Column('time_range_end', sa.String(64), nullable=True),
        sa.Column('error', sa.String(), nullable=True),
        sa.Column('sampled_at', sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('data_census')
