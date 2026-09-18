"""add functions.bulk_history (series crawl mode, add-fund-crawl-control-center)

Revision ID: 003_function_bulk_history
Revises: 002_crawl_control_center
Create Date: 2026-08-08
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '003_function_bulk_history'
down_revision = '002_crawl_control_center'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'functions',
        sa.Column('bulk_history', sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column('functions', 'bulk_history')
