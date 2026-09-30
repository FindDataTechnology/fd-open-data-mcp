"""Concept-coverage cache table (panel-data-coverage-cache).

Revision ID: 0005_concept_coverage
Revises: 0004_scope_tables

One row per concept with observations, served to ``/panel/data`` and the
``data_stats`` unfiltered detail instead of a live point-grain aggregate over
``semantic_observations``. Written only by the hourly background refresh
(single-flight via advisory lock) and the manual panel refresh action.

Idempotent (IF NOT EXISTS), PostgreSQL-only like the rest of the chain —
SQLite test databases build their schema from the models with ``create_all``.
Downgrade drops the cache only; it is recomputable from live observations.
"""
from __future__ import annotations

from alembic import op

revision = "0005_concept_coverage"
down_revision = "0004_scope_tables"
branch_labels = None
depends_on = None

_DDL = (
    """CREATE TABLE IF NOT EXISTS concept_coverage (
  concept_id INTEGER PRIMARY KEY
    REFERENCES concepts(id) ON DELETE CASCADE,
  rows INTEGER NOT NULL,
  latest_date VARCHAR(64),
  last_fetch TIMESTAMP,
  sources INTEGER NOT NULL DEFAULT 0,
  computed_at TIMESTAMP NOT NULL DEFAULT NOW()
)""",
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return  # sqlite tests build via create_all (chain convention)
    for statement in _DDL:
        bind.exec_driver_sql(statement)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    bind.exec_driver_sql("DROP TABLE IF EXISTS concept_coverage")
