"""add crawl_policies executor/script/script_args (add-direct-script-executor)

Direct policies run a Python script (mounted via ConfigMap) instead of the
Scrapy concept_crawl spider, through the same policy/reconciler/multi-cluster
dispatch path.

Revision ID: 007_crawl_policy_direct_executor
Revises: 006_function_bulk_snapshot
Create Date: 2026-08-27
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '007_crawl_policy_direct_executor'
down_revision = '006_function_bulk_snapshot'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('crawl_policies', sa.Column(
        'executor', sa.String(length=16), nullable=False,
        server_default=sa.text("'scrapy'")))
    op.add_column('crawl_policies', sa.Column('script', sa.String(length=255),
                                             nullable=True))
    op.add_column('crawl_policies', sa.Column(
        'script_args', sa.dialects.postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column('crawl_policies', 'script_args')
    op.drop_column('crawl_policies', 'script')
    op.drop_column('crawl_policies', 'executor')
