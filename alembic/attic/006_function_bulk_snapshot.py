"""add functions.bulk_snapshot (fix-silent-zero-yield-crawls)

Mirrors bulk_history: marks functions whose single call returns the full
entity cross-section for one date (e.g. fund_open_fund_daily_em), letting the
planner emit ONE cell per date instead of one per entity.

Revision ID: 006_function_bulk_snapshot
Revises: 005_policy_run_yield
Create Date: 2026-08-27
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '006_function_bulk_snapshot'
down_revision = '005_policy_run_yield'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'functions',
        sa.Column('bulk_snapshot', sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column('functions', 'bulk_snapshot')
