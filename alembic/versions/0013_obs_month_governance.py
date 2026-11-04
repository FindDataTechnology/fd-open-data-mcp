r"""Observation date governance: derived partition bucket + fetched_at index.

Revision ID: 0013_obs_month_governance
Revises: 0012_price_active_pool

add-data-stream-engine task 2.2 (census 2026-11-04, see
reports/2026-11-04-capacity-census.md). Three changes, all PostgreSQL-only
(SQLite test consumers build from the models with create_all):

1. ``semantic_observations.obs_month`` — derived month bucket 'YYYY-MM'
   computed from ``date`` WITHOUT rewriting any stored value. Census found
   six date shapes in production (canonical 99.6%, bare years from a
   cnreport writer defect, hour-truncated timestamps, bare months, ISO
   weeks, one 'LATEST' sentinel); the read path deliberately tolerates bare
   forms, so governance materializes a bucket column instead of rewriting
   values (rewrites risk uq_sem_obs collisions). NULL = unmappable → the
   default partition once monthly RANGE partitions exist (task 2.6).

2. ``ix_semantic_observations_fetched_at`` — the panel's staleness checks
   and daily-increment statistics seq-scanned 6.5M rows without it.

3. ``semantic_observations_date_shape_check`` (NOT VALID) — write gate
   against NEW garbage: every shape found in production is whitelisted
   (canonical, bare year, bare month, hour-truncated, ISO week); anything
   else ('LATEST'-class sentinels and arbitrary junk) is rejected for new
   rows only. NOT VALID leaves existing rows untouched — zero rewrite, and
   the crawl-fleet writer image does not need a redeploy to comply.

The backfill is batched by id (100k) to keep transactions short on the live
table; each batch is an independent commit so a crash resumes by rerunning
the migration's UPDATE (it is idempotent — the CASE is deterministic).
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0013_obs_month_governance"
down_revision = "0012_price_active_pool"
branch_labels = None
depends_on = None

_BATCH = 100_000

# Mirrors fd_open_data_mcp.semantic.dates.obs_month_of — keep in sync.
# Week rows (7 in production) map via ISO-week Monday month; expressed in
# pure SQL to stay inside one UPDATE.
_BACKFILL_SQL = """
UPDATE semantic_observations SET obs_month = sub.m
FROM (SELECT id AS sid, CASE
        WHEN date ~ '^\\d{4}-W\\d{2}$' THEN
            to_char(to_date(date || '-1', 'IYYY-"W"IW-ID'), 'YYYY-MM')
        WHEN date ~ '^\\d{4}$' THEN date || '-12'
        WHEN date ~ '^\\d{4}-\\d{2}-\\d{2}T' THEN substr(date, 1, 7)
        WHEN date ~ '^\\d{4}-\\d{2}(-\\d{2})?.*$' THEN substr(date, 1, 7)
        ELSE NULL
      END AS m
      FROM semantic_observations WHERE id BETWEEN :lo AND :hi) sub
WHERE semantic_observations.id = sub.sid
"""

_SHAPE_CHECK = (
    "date ~ '^\\d{4}-\\d{2}-\\d{2}$' OR "
    "date ~ '^\\d{4}$' OR "
    "date ~ '^\\d{4}-\\d{2}$' OR "
    "date ~ '^\\d{4}-\\d{2}-\\d{2}T' OR "
    "date ~ '^\\d{4}-W\\d{2}$'"
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.add_column(
        "semantic_observations",
        sa.Column("obs_month", sa.String(length=7), nullable=True),
    )

    # Batched backfill: short transactions on the live table.
    lo = bind.execute(sa.text("SELECT min(id) FROM semantic_observations")).scalar()
    hi = bind.execute(sa.text("SELECT max(id) FROM semantic_observations")).scalar()
    if lo is not None and hi is not None:
        cur = lo
        while cur <= hi:
            bind.execute(
                sa.text(_BACKFILL_SQL), {"lo": cur, "hi": cur + _BATCH - 1}
            )
            cur += _BATCH

    op.create_index(
        "ix_semantic_observations_fetched_at", "semantic_observations",
        ["fetched_at"], unique=False,
    )
    # NOT VALID: existing rows unchecked (the 'LATEST' sentinel stays until
    # its own cleanup), every NEW row must match a tolerated shape.
    op.create_check_constraint(
        "semantic_observations_date_shape_check", "semantic_observations",
        _SHAPE_CHECK, postgresql_not_valid=True,
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.drop_constraint("semantic_observations_date_shape_check",
                       "semantic_observations", type_="check")
    op.drop_index("ix_semantic_observations_fetched_at",
                  table_name="semantic_observations")
    op.drop_column("semantic_observations", "obs_month")
