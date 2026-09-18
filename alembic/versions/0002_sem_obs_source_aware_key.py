"""Observation unique key gains source_used (add-multi-source-observations).

Revision ID: 0002_sem_obs_source_aware_key
Revises: 0001_schema_baseline

Two sources' values for the same point (WorldBank's China-2024 GDP and NBS's)
coexist as separate rows instead of first-writer-wins; reads pick the
preferred source at query time via ``source_rankings``. Values are never
merged or overwritten across sources.

Data safety / expand-contract: the 6-column key is a strict superset of the
old 5-column one and ``source_used`` is NOT NULL on every row, so no existing
row can violate it — the swap cannot fail on data and rewrites nothing. The
write paths (old and new) are ORM select-then-update/insert with no
``ON CONFLICT`` target, so the previously deployed release keeps working
against the relaxed key for both reads and writes.

Ported from ``fd_open_data_mcp.migrate._swap_sem_obs_key_postgres`` (retired
by this change) and the manual runbook
``docs/migrations-archive/007_multi_source_observations.sql``. Takes the same
advisory lock the runbook used (``fd_mcp_uq_sem_obs_swap``), so this revision
cannot race a leftover manual execution, and keeps the same re-entrancy: an
INVALID index from an interrupted CONCURRENTLY build is dropped first, and a
database already carrying the 6-column key (swapped manually before rollout)
is a no-op.

Ops note (carried from the runbook): the coordinator's dedup view
``semantic_observations_read`` is defined outside this repo; after this
revision applies, update it to dedup per source — until then view-path reads
return the view's chosen row, which degrades visibility but not correctness.

PostgreSQL only (CONCURRENTLY cannot run inside a transaction, hence the
autocommit block). Downgrade is not implemented: the reverse direction is
lossy (multiple rows per old key cannot coexist), rollback is redeploying
the previous image — for a manual lossy reverse see
``docs/migrations-archive/007_multi_source_observations.rollback.sql``.
"""
from __future__ import annotations

from alembic import op

revision = "0002_sem_obs_source_aware_key"
down_revision = "0001_schema_baseline"
branch_labels = None
depends_on = None

_ADVISORY_LOCK_KEY = "fd_mcp_uq_sem_obs_swap"
_KEY_COLS = ("concept_id", "entity_type", "entity_id", "date", "granularity",
             "source_used")


def _uq_sem_obs_columns(bind) -> set[str]:
    """Column names of the ``uq_sem_obs`` constraint ([] when absent)."""
    rows = bind.exec_driver_sql(
        "SELECT a.attname "
        "FROM pg_constraint c "
        "JOIN pg_class t ON t.oid = c.conrelid "
        "CROSS JOIN LATERAL unnest(c.conkey) AS k(attnum) "
        "JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum "
        "WHERE c.conrelid = 'semantic_observations'::regclass "
        "AND c.conname = 'uq_sem_obs'"
    )
    return {r[0] for r in rows}


def upgrade() -> None:
    bind = op.get_bind()
    if _uq_sem_obs_columns(bind) == set(_KEY_COLS):
        return  # already source-aware (e.g. swapped via the 007 runbook)

    with op.get_context().autocommit_block():
        bind.exec_driver_sql(
            f"SELECT pg_advisory_lock(hashtext('{_ADVISORY_LOCK_KEY}'))"
        )
        try:
            # A failed CONCURRENTLY build leaves an INVALID index behind.
            invalid = bind.exec_driver_sql(
                "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE c.relname = 'uq_sem_obs_src' AND NOT i.indisvalid"
            ).scalar()
            if invalid:
                bind.exec_driver_sql(
                    "DROP INDEX CONCURRENTLY IF EXISTS uq_sem_obs_src"
                )
            bind.exec_driver_sql(
                "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_sem_obs_src "
                f"ON semantic_observations ({', '.join(_KEY_COLS)})"
            )
            # The old key may exist as a table constraint or as a standalone
            # index (005-era databases); drop both forms before attaching.
            bind.exec_driver_sql(
                "ALTER TABLE semantic_observations DROP CONSTRAINT IF EXISTS uq_sem_obs"
            )
            bind.exec_driver_sql("DROP INDEX IF EXISTS uq_sem_obs")
            bind.exec_driver_sql(
                "ALTER TABLE semantic_observations "
                "ADD CONSTRAINT uq_sem_obs UNIQUE USING INDEX uq_sem_obs_src"
            )
        finally:
            bind.exec_driver_sql(
                f"SELECT pg_advisory_unlock(hashtext('{_ADVISORY_LOCK_KEY}'))"
            )


def downgrade() -> None:
    raise NotImplementedError(
        "downgrade of the source-aware key is lossy (multiple rows per old "
        "key cannot coexist); rollback is redeploying the previous image. "
        "For a manual lossy reverse see "
        "docs/migrations-archive/007_multi_source_observations.rollback.sql"
    )
