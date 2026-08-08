"""Add crawl_policies and policy_runs tables for the crawl control center.

Revision ID: 002_crawl_control_center
Revises: 001_add_entity_tables
Create Date: 2026-08-08

Part of openspec change `add-fund-crawl-control-center` (design D4).
Adds:
- crawl_policies table (scope-bearing recurring crawls: concepts x entities x
  date policy x frequency, executed by the reconciler)
- policy_runs table (execution audit: status, compiled plan, job_ref)
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = '002_crawl_control_center'
down_revision = '001_add_entity_tables'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add crawl_policies and policy_runs tables."""

    op.create_table(
        'crawl_policies',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=128), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('concept_ids', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('entity_type', sa.String(length=32), nullable=False),
        sa.Column('entity_ids', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('date_policy', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('frequency', sa.String(length=32), nullable=False, server_default='daily'),
        sa.Column('mode', sa.String(length=16), nullable=False, server_default='per_date'),
        sa.Column('source_filter', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('force', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('cron_expr', sa.String(length=128), nullable=False),
        sa.Column('timezone', sa.String(length=64), nullable=False, server_default='UTC'),
        sa.Column('last_run_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name', name='uq_crawl_policies_name'),
    )
    op.create_index('ix_crawl_policies_name', 'crawl_policies', ['name'], unique=False)

    op.create_table(
        'policy_runs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('policy_id', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=32), nullable=False, server_default='running'),
        sa.Column('plan_json', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('job_ref', sa.String(length=255), nullable=True),
        sa.Column('started_at', sa.DateTime(), nullable=False),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
        sa.Column('detail', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['policy_id'], ['crawl_policies.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_policy_runs_policy_id', 'policy_runs', ['policy_id'], unique=False)


def downgrade() -> None:
    """Remove crawl_policies and policy_runs tables."""

    op.drop_index('ix_policy_runs_policy_id', table_name='policy_runs')
    op.drop_table('policy_runs')

    op.drop_index('ix_crawl_policies_name', table_name='crawl_policies')
    op.drop_table('crawl_policies')
