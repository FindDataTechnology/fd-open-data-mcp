"""Scope tables for the indicator-scope capability (registry-transparent-read-and-scope).

Revision ID: 0004_scope_tables
Revises: 0003_pgvector_embedding_columns

Creates the three storage tables of the retrieval-scope system (design
D3/D5/D6):

  - ``scopes``          named allow-list objects, rules as one JSONB object
  - ``scope_bindings``  caller -> default scope (opaque caller_key)
  - ``scope_stats``     per-(scope, day) hit counters, incremented per call

Idempotent (IF NOT EXISTS), PostgreSQL-only like the rest of the chain —
SQLite test databases build their schema from the models with ``create_all``.
Downgrade drops the three tables only; scope names carry no other
dependencies (bindings are cleaned up by ``scope_delete`` at the
application layer, so no FK is declared here — a dangling binding is
resolved as "no default" by the reader).
"""
from __future__ import annotations

from alembic import op

revision = "0004_scope_tables"
down_revision = "0003_pgvector_embedding_columns"
branch_labels = None
depends_on = None

_DDL = (
    """CREATE TABLE IF NOT EXISTS scopes (
  id SERIAL PRIMARY KEY,
  name VARCHAR(128) NOT NULL UNIQUE,
  description TEXT,
  rules JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMP DEFAULT NOW(),
  updated_at TIMESTAMP DEFAULT NOW()
)""",
    """CREATE TABLE IF NOT EXISTS scope_bindings (
  caller_key VARCHAR(255) PRIMARY KEY,
  scope_name VARCHAR(128) NOT NULL,
  created_at TIMESTAMP DEFAULT NOW(),
  updated_at TIMESTAMP DEFAULT NOW()
)""",
    "CREATE INDEX IF NOT EXISTS ix_scope_bindings_scope_name ON scope_bindings (scope_name)",
    """CREATE TABLE IF NOT EXISTS scope_stats (
  scope_name VARCHAR(128) NOT NULL,
  day VARCHAR(10) NOT NULL,
  calls INTEGER NOT NULL DEFAULT 0,
  results_returned INTEGER NOT NULL DEFAULT 0,
  CONSTRAINT pk_scope_stats_scope_day PRIMARY KEY (scope_name, day)
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
    bind.exec_driver_sql("DROP TABLE IF EXISTS scope_stats")
    bind.exec_driver_sql("DROP INDEX IF EXISTS ix_scope_bindings_scope_name")
    bind.exec_driver_sql("DROP TABLE IF EXISTS scope_bindings")
    bind.exec_driver_sql("DROP TABLE IF EXISTS scopes")
