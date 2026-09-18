"""add policy_runs yield counters (fix-silent-zero-yield-crawls)

plan_cells recorded at launch; rows_attempted/rows_new updated incrementally
by the pod on each pipeline flush keyed by SCRAW_JOB_REF. Nullable ints:
absent means "the pod never reported" — which the reconciler classifies as
zero_yield (design D1/D2/D3).

Revision ID: 005_policy_run_yield
Revises: 004_fetch_log_function_cluster
Create Date: 2026-08-27
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '005_policy_run_yield'
down_revision = '004_fetch_log_function_cluster'
branch_labels = None
depends_on = None


def upgrade() -> None:
    for col in ('plan_cells', 'rows_attempted', 'rows_new'):
        op.add_column('policy_runs', sa.Column(col, sa.Integer(), nullable=True))


def downgrade() -> None:
    for col in ('rows_new', 'rows_attempted', 'plan_cells'):
        op.drop_column('policy_runs', col)
