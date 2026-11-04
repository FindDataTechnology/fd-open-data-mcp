r"""Concept rollup tables: concept_source_stats + coverage.coexisting_points.

Revision ID: 0014_concept_rollups
Revises: 0013_obs_month_governance

add-data-stream-engine tasks 2.3/2.4 (census 2026-11-04). The write-path
rollup that retires the hourly full-table coverage refresh (~102 s at 6.3M
rows, scaling linearly — the structural fix):

- ``concept_source_stats``: one row per (concept_id, source_used) — rows,
  latest_date, last_fetch. Maintained INCREMENTALLY by the write hook
  (visibility.coverage.bump_rollups) inside the observation-write
  transaction; cost O(batch), never O(table).
- ``concept_coverage.coexisting_points``: cross-source duplicate-point
  counter keeping the "coexisting sources count as ONE point" caliber exact
  under incremental maintenance.

The migration backfills BOTH tables from row-level observations with two
set-based statements (INSERT...SELECT + one five-column GROUP BY for the
coexistence counts) — 6.5M rows aggregate in seconds, and the backfill is
idempotent: rerunning is the same as ``fd-open-data-mcp rebuild-rollups``.

PostgreSQL-only like the rest of the chain (SQLite test consumers build
from the models with create_all; the hook + rebuild cover them).
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0014_concept_rollups"
down_revision = "0013_obs_month_governance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.create_table(
        "concept_source_stats",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("concept_id", sa.Integer(), nullable=False),
        sa.Column("source_used", sa.String(length=64), nullable=False),
        sa.Column("rows", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("latest_date", sa.String(length=64), nullable=True),
        sa.Column("last_fetch", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["concept_id"], ["concepts.id"],
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("concept_source_stats_pkey")),
        sa.UniqueConstraint("concept_id", "source_used", name="uq_concept_source"),
    )
    op.create_index("ix_concept_source_stats_concept_id",
                    "concept_source_stats", ["concept_id"], unique=False)

    op.add_column(
        "concept_coverage",
        sa.Column("coexisting_points", sa.Integer(), nullable=False,
                  server_default="0"),
    )

    # Backfill the pairs table...
    bind.exec_driver_sql("""
        INSERT INTO concept_source_stats
            (concept_id, source_used, rows, latest_date, last_fetch)
        SELECT concept_id, source_used, count(*), max(date), max(fetched_at)
        FROM semantic_observations
        GROUP BY concept_id, source_used
        ON CONFLICT (concept_id, source_used) DO UPDATE
            SET rows = EXCLUDED.rows,
                latest_date = EXCLUDED.latest_date,
                last_fetch = EXCLUDED.last_fetch
    """)
    # ...and the exact coexistence counter (five-column GROUP BY — the same
    # point machinery coverage_by_concept uses; ~8 s at 6.3M rows).
    bind.exec_driver_sql("""
        UPDATE concept_coverage c
        SET coexisting_points = COALESCE(dup.n, 0)
        FROM (
            SELECT concept_id, SUM(srcs - 1) AS n FROM (
                SELECT concept_id, entity_type, entity_id, date, granularity,
                       count(*) AS srcs
                FROM semantic_observations
                GROUP BY concept_id, entity_type, entity_id, date, granularity
                HAVING count(*) > 1
            ) dups GROUP BY concept_id
        ) dup
        WHERE c.concept_id = dup.concept_id
    """)
    # Project every coverage row from the freshly filled stats (rows /
    # latest_date / last_fetch / sources), keeping the pre-existing caliber.
    bind.exec_driver_sql("""
        UPDATE concept_coverage c
        SET rows = COALESCE(s.total, 0) - c.coexisting_points,
            latest_date = s.top_date,
            last_fetch = s.top_fetch,
            sources = s.pair_count,
            computed_at = now()
        FROM (
            SELECT concept_id,
                   SUM(rows) AS total,
                   max(latest_date) AS top_date,
                   max(last_fetch) AS top_fetch,
                   count(*) AS pair_count
            FROM concept_source_stats
            GROUP BY concept_id
        ) s
        WHERE c.concept_id = s.concept_id
    """)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.drop_column("concept_coverage", "coexisting_points")
    op.drop_index("ix_concept_source_stats_concept_id",
                  table_name="concept_source_stats")
    op.drop_table("concept_source_stats")
