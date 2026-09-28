"""pgvector columns for the two JSON embedding tables + registry embeddings.

Revision ID: 0003_pgvector_embedding_columns
Revises: 0002_sem_obs_source_aware_key

mcp-search-engine-overhaul task 3.2. The embedding tables store vectors as
JSON text (``concept_embeddings.embedding`` JSONB, ``entity_embeddings.embedding``
TEXT) and every query pays a Python brute-force cosine over the full table.
This revision adds a native ``vector(384)`` column next to each JSON column
(dimension of all-MiniLM-L6-v2, see ``fd_open_data_mcp/embeddings/model.py``),
backfills it from the JSON payload, and builds HNSW cosine indexes. It also
creates ``registry_indicator_embeddings`` (the same dual JSONB + vector shape
for registry entries) whose DDL is a cross-task contract — other agents code
against it verbatim, do not reshape it.

Expand-contract: the JSON columns stay (readers keep working); the new columns
are nullable and backfilled in batches of 1000 inside an autocommit block, so
each batch commits on its own and the migration never holds one long
transaction over the whole table. Every statement is idempotent
(IF NOT EXISTS / WHERE embedding_vec IS NULL), so an interrupted run is
resumed, not restarted, and a re-run is a no-op.

PostgreSQL only (vector type, HNSW, ``<=>``). Non-postgresql dialects get a
no-op upgrade/downgrade — the same convention as the baseline: SQLite test
consumers build their schema from the models with ``create_all``, never from
this chain. Downgrade drops the indexes, the vector columns and the new
table; it does NOT touch ``registry_entries`` and does NOT drop the vector
extension (other databases on the cluster may depend on it).
"""
from __future__ import annotations

from alembic import op

revision = "0003_pgvector_embedding_columns"
down_revision = "0002_sem_obs_source_aware_key"
branch_labels = None
depends_on = None

_EMBEDDING_DIM = 384  # all-MiniLM-L6-v2 (fd_open_data_mcp.embeddings.model)
_BACKFILL_BATCH = 1000

# Cross-task contract (mcp-search-engine-overhaul): other agents code against
# this DDL verbatim. registry_entries already exists in the authoritative
# database and is deliberately not created here.
_REGISTRY_DDL = """CREATE TABLE IF NOT EXISTS registry_indicator_embeddings (
  id SERIAL PRIMARY KEY,
  registry_entry_id BIGINT NOT NULL UNIQUE REFERENCES registry_entries(id) ON DELETE CASCADE,
  embedding JSONB NOT NULL,
  embedding_vec vector(384),
  model VARCHAR(128) NOT NULL,
  embedded_text TEXT,
  updated_at TIMESTAMP DEFAULT NOW(),
  UNIQUE (model, registry_entry_id)
);"""

# Backfill batches: each pass converts at most _BACKFILL_BATCH rows from the
# JSON payload. concept_embeddings.embedding is JSONB (needs the ::text hop),
# entity_embeddings.embedding is TEXT (casts straight to vector).
_BACKFILL_SQL = (
    "WITH batch AS ("
    " SELECT id FROM {table} WHERE embedding_vec IS NULL LIMIT {limit}"
    ") "
    "UPDATE {table} t SET embedding_vec = {cast} "
    "FROM batch WHERE t.id = batch.id"
)
_BACKFILLS = (
    ("concept_embeddings", "t.embedding::text::vector"),
    ("entity_embeddings", "t.embedding::vector"),
)

_HNSW_INDEXES = (
    "CREATE INDEX IF NOT EXISTS hnsw_concept_embeddings_vec "
    "ON concept_embeddings USING hnsw (embedding_vec vector_cosine_ops)",
    "CREATE INDEX IF NOT EXISTS hnsw_entity_embeddings_vec "
    "ON entity_embeddings USING hnsw (embedding_vec vector_cosine_ops)",
    "CREATE INDEX IF NOT EXISTS hnsw_registry_indicator_embeddings_vec "
    "ON registry_indicator_embeddings USING hnsw (embedding_vec vector_cosine_ops)",
)


def _is_postgresql(bind) -> bool:
    return bind.dialect.name == "postgresql"


def _backfill(bind) -> None:
    """Convert JSON embeddings to vectors in committed batches of 1000."""
    for table, cast in _BACKFILLS:
        converted = 0
        while True:
            result = bind.exec_driver_sql(
                _BACKFILL_SQL.format(table=table, cast=cast, limit=_BACKFILL_BATCH)
            )
            n = result.rowcount or 0
            converted += n
            if n < _BACKFILL_BATCH:
                break  # table drained (or cast produced no rows)
        print(
            f"  backfilled {converted} {table} rows into embedding_vec "
            f"(batches of {_BACKFILL_BATCH})"
        )


def upgrade() -> None:
    bind = op.get_bind()
    if not _is_postgresql(bind):
        return  # PG-only (vector type / HNSW); sqlite tests build via create_all

    # Autocommit so each batch commits on its own — no long transaction over
    # the embedding tables (same mechanism revision 0002 uses for CONCURRENTLY).
    with op.get_context().autocommit_block():
        bind.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS vector")
        bind.exec_driver_sql(
            "ALTER TABLE concept_embeddings "
            f"ADD COLUMN IF NOT EXISTS embedding_vec vector({_EMBEDDING_DIM})"
        )
        bind.exec_driver_sql(
            "ALTER TABLE entity_embeddings "
            f"ADD COLUMN IF NOT EXISTS embedding_vec vector({_EMBEDDING_DIM})"
        )
        bind.exec_driver_sql(_REGISTRY_DDL)
        _backfill(bind)
        # Indexes after the backfill: HNSW maintenance during a bulk fill is
        # far slower than one post-fill build.
        for statement in _HNSW_INDEXES:
            bind.exec_driver_sql(statement)


def downgrade() -> None:
    bind = op.get_bind()
    if not _is_postgresql(bind):
        return

    with op.get_context().autocommit_block():
        for statement in _HNSW_INDEXES:
            # each entry is "CREATE INDEX IF NOT EXISTS <name> ON ..."
            name = statement.split()[5]
            bind.exec_driver_sql(f"DROP INDEX IF EXISTS {name}")
        bind.exec_driver_sql(
            "ALTER TABLE concept_embeddings DROP COLUMN IF EXISTS embedding_vec"
        )
        bind.exec_driver_sql(
            "ALTER TABLE entity_embeddings DROP COLUMN IF EXISTS embedding_vec"
        )
        bind.exec_driver_sql("DROP TABLE IF EXISTS registry_indicator_embeddings")
        # registry_entries and the vector extension are deliberately untouched.
