r"""Storage watermark: obs_month bucket distribution on the census row.

Revision ID: 0015_obs_month_dist
Revises: 0014_concept_rollups

add-data-stream-engine task 2.7. The watermark page must show the obs_month
bucket distribution (the future default-partition territory: how many rows
map to NULL because their date is a sentinel/garbage) WITHOUT re-scanning
6.5M rows on every page view. The census refresh (already an explicit
action, already doing one full count(*) scan) computes the distribution once
per cycle and caches it here as JSONB on the local store's row; the page
reads the cached value.

PostgreSQL-only like the rest of the chain (SQLite test consumers build from
the models with create_all; the distribution probe in census.py is guarded so
a SQLite store simply records NULL).
"""
from __future__ import annotations

from alembic import op

revision = "0015_obs_month_dist"
down_revision = "0014_concept_rollups"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    bind.exec_driver_sql(
        "ALTER TABLE data_census ADD COLUMN obs_month_dist JSONB"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    bind.exec_driver_sql("ALTER TABLE data_census DROP COLUMN obs_month_dist")
