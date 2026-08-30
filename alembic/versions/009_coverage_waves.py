"""coverage_waves bookkeeping table (expand-crawl-coverage)

One row per backfill wave driven by the coverage expander: the gap slice
(entity_type x frequency x coverage_state), the materialized backfill
policies, and the gate state (planned/running/verifying/done/paused) with
before/after coverage deltas.

Revision ID: 009_coverage_waves
Revises: 008_data_census
Create Date: 2026-08-30
"""
from alembic import op
import sqlalchemy as sa

from fd_open_data_mcp.models import JSONB

# revision identifiers, used by Alembic.
revision = '009_coverage_waves'
down_revision = '008_data_census'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'coverage_waves',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('entity_type', sa.String(32), nullable=False, index=True),
        sa.Column('frequency_bucket', sa.String(32), nullable=False),
        sa.Column('coverage_state', sa.String(16), nullable=False),
        sa.Column('concept_ids', JSONB, nullable=False),
        sa.Column('date_policy', JSONB, nullable=False),
        sa.Column('mode', sa.String(16), nullable=False, server_default='per_date'),
        sa.Column('status', sa.String(16), nullable=False, server_default='planned',
                  index=True),
        sa.Column('policy_ids', JSONB, nullable=True),
        sa.Column('rows_new', sa.Integer(), nullable=True),
        sa.Column('concepts_before', sa.Integer(), nullable=True),
        sa.Column('concepts_after', sa.Integer(), nullable=True),
        sa.Column('detail', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table('coverage_waves')
