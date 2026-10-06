"""Revision 0003_pgvector_embedding_columns — contract and guard tests.

The revision targets PostgreSQL 16 + pgvector, which the test environment
does not have, so these tests pin it down two other ways:

- Bind-level: ``upgrade()``/``downgrade()`` bound to a real SQLite connection
  must execute nothing (the dialect guard), matching the convention that the
  sqlite test world builds its schema with ``create_all`` and never from this
  chain.
- Fake-PG-bind: a recording bind that claims to be postgresql must receive
  the contract SQL (verbatim registry DDL, both vector columns, batched
  backfill casts, the three HNSW indexes after the backfill, downgrade drops
  without touching registry_entries or the extension).
- CLI: the acceptance command itself — ``alembic upgrade head`` on a fresh
  sqlite database — runs green (all three revisions are guarded no-ops).
"""
from __future__ import annotations

import contextlib
import importlib.util
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event, text

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_DIR = PROJECT_ROOT / "alembic"
REVISION_PATH = ALEMBIC_DIR / "versions" / "0003_pgvector_embedding_columns.py"

REGISTRY_DDL_CONTRACT = """CREATE TABLE IF NOT EXISTS registry_indicator_embeddings (
  id SERIAL PRIMARY KEY,
  registry_entry_id BIGINT NOT NULL UNIQUE REFERENCES registry_entries(id) ON DELETE CASCADE,
  embedding JSONB NOT NULL,
  embedding_vec vector(384),
  model VARCHAR(128) NOT NULL,
  embedded_text TEXT,
  updated_at TIMESTAMP DEFAULT NOW(),
  UNIQUE (model, registry_entry_id)
);"""


def _load_revision():
    spec = importlib.util.spec_from_file_location("mig_0003", REVISION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mig():
    return _load_revision()


# ---------------------------------------------------------------------------
# Chain wiring
# ---------------------------------------------------------------------------

def test_revision_is_new_head_of_shipped_chain():
    """The shipped head is 0007 (legal-line federation registration); each
    revision keeps its own down_revision wiring (their contract tests live
    above)."""
    script = ScriptDirectory(str(ALEMBIC_DIR))
    assert script.get_current_head() == "0007_legal_federation"
    rev = script.get_revision("0007_legal_federation")
    assert rev.down_revision == "0006_control_plane_adoption"
    rev6 = script.get_revision("0006_control_plane_adoption")
    assert rev6.down_revision == "0005_concept_coverage"
    rev5 = script.get_revision("0005_concept_coverage")
    assert rev5.down_revision == "0004_scope_tables"
    rev4 = script.get_revision("0004_scope_tables")
    assert rev4.down_revision == "0003_pgvector_embedding_columns"


def test_chain_is_linear_head_to_baseline():
    script = ScriptDirectory(str(ALEMBIC_DIR))
    revisions = [r.revision for r in script.walk_revisions()]
    assert revisions == [
        "0007_legal_federation",
        "0006_control_plane_adoption",
        "0005_concept_coverage",
        "0004_scope_tables",
        "0003_pgvector_embedding_columns",
        "0002_sem_obs_source_aware_key",
        "0001_schema_baseline",
    ]


# ---------------------------------------------------------------------------
# SQLite guard: bound to a real sqlite connection, executes nothing
# ---------------------------------------------------------------------------

def _sqlite_ops(monkeypatch, mig):
    engine = create_engine("sqlite://")
    conn = engine.connect()
    ctx = MigrationContext.configure(conn)
    monkeypatch.setattr(mig, "op", Operations(ctx))

    executed = []
    event.listen(conn, "before_cursor_execute", lambda c, *a, **k: executed.append(a[1]))
    return engine, conn, executed


def test_upgrade_is_noop_on_sqlite(mig, monkeypatch):
    engine, conn, executed = _sqlite_ops(monkeypatch, mig)
    try:
        mig.upgrade()
        assert executed == [], "upgrade() must not touch a non-postgresql database"
    finally:
        conn.close()
        engine.dispose()


def test_downgrade_is_noop_on_sqlite(mig, monkeypatch):
    engine, conn, executed = _sqlite_ops(monkeypatch, mig)
    try:
        mig.downgrade()
        assert executed == [], "downgrade() must not touch a non-postgresql database"
    finally:
        conn.close()
        engine.dispose()


def test_guard_is_the_first_thing_upgrade_does(mig):
    """Even a broken bind never reaches SQL when the dialect is not PG."""
    broken = types.SimpleNamespace(
        dialect=types.SimpleNamespace(name="mysql"),
        exec_driver_sql=lambda *_: pytest.fail("guard must return before any SQL"),
    )
    fake_op = types.SimpleNamespace(
        get_bind=lambda: broken,
        get_context=lambda: pytest.fail("guard must return before context use"),
    )
    original = mig.op
    mig.op = fake_op
    try:
        mig.upgrade()  # must simply return
        mig.downgrade()
    finally:
        mig.op = original


# ---------------------------------------------------------------------------
# Fake PG bind: the SQL the revision must emit
# ---------------------------------------------------------------------------

class _FakeResult:
    def __init__(self, rowcount):
        self.rowcount = rowcount


class _Recorder:
    """Bind impersonating postgresql; records exec_driver_sql statements."""

    def __init__(self, rowcounts=None):
        self.dialect = types.SimpleNamespace(name="postgresql")
        self.statements = []
        self._rowcounts = list(rowcounts or [])

    def exec_driver_sql(self, sql):
        self.statements.append(sql)
        if sql.lstrip().startswith("WITH batch AS"):
            # only backfill passes read their rowcount from the script
            return _FakeResult(self._rowcounts.pop(0) if self._rowcounts else 0)
        return _FakeResult(0)


def _run_on_fake_pg(mig, rowcounts=None):
    bind = _Recorder(rowcounts)
    fake_op = types.SimpleNamespace(
        get_bind=lambda: bind,
        get_context=lambda: types.SimpleNamespace(
            autocommit_block=lambda: contextlib.nullcontext()
        ),
    )
    original = mig.op
    mig.op = fake_op
    try:
        mig.upgrade()
    finally:
        mig.op = original
    return bind


def test_upgrade_pg_branch_emits_contract_sql_in_order(mig):
    bind = _run_on_fake_pg(mig)
    stmts = bind.statements

    def first_index_of(fragment):
        return next(i for i, s in enumerate(stmts) if fragment in s)

    ext = first_index_of("CREATE EXTENSION IF NOT EXISTS vector")
    col_concept = first_index_of(
        "ALTER TABLE concept_embeddings ADD COLUMN IF NOT EXISTS embedding_vec vector(384)"
    )
    col_entity = first_index_of(
        "ALTER TABLE entity_embeddings ADD COLUMN IF NOT EXISTS embedding_vec vector(384)"
    )
    entries = first_index_of("CREATE TABLE IF NOT EXISTS registry_entries")
    entries_idx = first_index_of(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_registry_semantic_code"
    )
    registry = first_index_of("CREATE TABLE IF NOT EXISTS registry_indicator_embeddings")
    backfill_concept = first_index_of(
        "UPDATE concept_embeddings t SET embedding_vec = t.embedding::text::vector"
    )
    backfill_entity = first_index_of(
        "UPDATE entity_embeddings t SET embedding_vec = t.embedding::vector"
    )
    index_concept = first_index_of(
        "CREATE INDEX IF NOT EXISTS hnsw_concept_embeddings_vec "
        "ON concept_embeddings USING hnsw (embedding_vec vector_cosine_ops)"
    )
    index_entity = first_index_of(
        "CREATE INDEX IF NOT EXISTS hnsw_entity_embeddings_vec "
        "ON entity_embeddings USING hnsw (embedding_vec vector_cosine_ops)"
    )
    index_registry = first_index_of(
        "CREATE INDEX IF NOT EXISTS hnsw_registry_indicator_embeddings_vec "
        "ON registry_indicator_embeddings USING hnsw (embedding_vec vector_cosine_ops)"
    )
    # order: extension -> columns -> registry_entries -> rie (FK!) -> backfill
    # -> HNSW indexes. registry_entries MUST precede rie: the hard FK in
    # _REGISTRY_DDL resolves against it on a fresh replay.
    assert ext < col_concept < col_entity < registry
    assert entries < entries_idx < registry
    assert registry < backfill_concept < backfill_entity
    assert backfill_entity < index_concept < index_entity < index_registry


def test_registry_entries_created_guarded_before_rie(mig):
    """schema-drift-closure: 0003 owns registry_entries' guarded create (the
    0006 adoption could not — it runs after the FK). The DDL must be the
    production shape, including verified_at, which the old CI bootstrap had
    drifted and lacked."""
    bind = _run_on_fake_pg(mig)
    entries = next(
        s for s in bind.statements
        if s.startswith("CREATE TABLE IF NOT EXISTS registry_entries")
    )
    for fragment in (
        "verified_at   TIMESTAMPTZ",
        "CONSTRAINT uq_registry_anchor",
        "source_column TEXT NOT NULL DEFAULT ''",
    ):
        assert fragment in entries, fragment
    semantic_idx = next(
        s for s in bind.statements
        if s.startswith("CREATE UNIQUE INDEX IF NOT EXISTS uq_registry_semantic_code")
    )
    assert "WHERE semantic_code IS NOT NULL" in semantic_idx
    rie = next(
        s for s in bind.statements
        if s.startswith("CREATE TABLE IF NOT EXISTS registry_indicator_embeddings")
    )
    assert bind.statements.index(entries) < bind.statements.index(rie)
    assert bind.statements.index(semantic_idx) < bind.statements.index(rie)


def test_registry_ddl_matches_contract_verbatim(mig):
    bind = _run_on_fake_pg(mig)
    ddl = next(s for s in bind.statements if s.startswith("CREATE TABLE IF NOT EXISTS registry_indicator_embeddings"))
    normalize = lambda s: " ".join(s.split())
    assert normalize(ddl) == normalize(REGISTRY_DDL_CONTRACT)
    # contract details the normalization could hide
    assert "vector(384)" in ddl
    assert "REFERENCES registry_entries(id) ON DELETE CASCADE" in ddl
    assert "UNIQUE (model, registry_entry_id)" in ddl
    assert "CREATE TABLE" not in ddl.replace("CREATE TABLE IF NOT EXISTS registry_indicator_embeddings", "")


def test_backfill_batches_of_1000_until_drained(mig):
    # per table: two full batches, then a partial one (37 < 1000 => drained,
    # no extra draining pass needed since the CTE LIMIT bounds selection)
    bind = _run_on_fake_pg(mig, rowcounts=[1000, 1000, 37] * 2)
    backfills = [s for s in bind.statements if s.lstrip().startswith("WITH batch AS")]
    assert len(backfills) == 6, "expected 3 passes per table (1000, 1000, 37)"
    assert all("LIMIT 1000" in s for s in backfills)
    assert all("WHERE embedding_vec IS NULL" in s for s in backfills), (
        "each pass must only convert rows not yet backfilled (resumable)"
    )
    concept = [s for s in backfills if "concept_embeddings" in s]
    entity = [s for s in backfills if "entity_embeddings" in s]
    assert len(concept) == len(entity) == 3
    assert all("t.embedding::text::vector" in s for s in concept), (
        "JSONB column needs the ::text hop before ::vector"
    )
    assert all("t.embedding::vector" in s for s in entity), "TEXT column casts directly"


def test_upgrade_is_idempotent(mig):
    # re-run: drains immediately (0 rows) and every DDL is IF NOT EXISTS
    _run_on_fake_pg(mig, rowcounts=[1000, 0, 1000, 0])
    bind = _run_on_fake_pg(mig)  # second run: all rowcounts default to 0
    backfills = [s for s in bind.statements if s.lstrip().startswith("WITH batch AS")]
    assert len(backfills) == 2, "empty backfill = one draining pass per table"
    for fragment in (
        "CREATE EXTENSION IF NOT EXISTS vector",
        "ADD COLUMN IF NOT EXISTS embedding_vec",
        "CREATE TABLE IF NOT EXISTS registry_indicator_embeddings",
        "CREATE INDEX IF NOT EXISTS hnsw_concept_embeddings_vec",
        "CREATE INDEX IF NOT EXISTS hnsw_entity_embeddings_vec",
        "CREATE INDEX IF NOT EXISTS hnsw_registry_indicator_embeddings_vec",
    ):
        assert any(fragment in s for s in bind.statements), fragment


def test_downgrade_pg_branch_drops_only_what_upgrade_added(mig):
    bind = _Recorder()
    fake_op = types.SimpleNamespace(
        get_bind=lambda: bind,
        get_context=lambda: types.SimpleNamespace(
            autocommit_block=lambda: contextlib.nullcontext()
        ),
    )
    original = mig.op
    mig.op = fake_op
    try:
        mig.downgrade()
    finally:
        mig.op = original

    joined = "\n".join(bind.statements)
    for drop in (
        "DROP INDEX IF EXISTS hnsw_concept_embeddings_vec",
        "DROP INDEX IF EXISTS hnsw_entity_embeddings_vec",
        "DROP INDEX IF EXISTS hnsw_registry_indicator_embeddings_vec",
        "ALTER TABLE concept_embeddings DROP COLUMN IF EXISTS embedding_vec",
        "ALTER TABLE entity_embeddings DROP COLUMN IF EXISTS embedding_vec",
        "DROP TABLE IF EXISTS registry_indicator_embeddings",
    ):
        assert drop in bind.statements, f"missing downgrade statement: {drop}"
    assert "DROP EXTENSION" not in joined, "extension is cluster-shared, never dropped"
    assert not any(
        "registry_entries" in s and "registry_indicator_embeddings" not in s
        for s in bind.statements
    ), "registry_entries must not be touched by downgrade"
    assert not any("DROP COLUMN" in s and "embedding_vec" not in s for s in bind.statements), (
        "only the vector columns are dropped; the JSON columns stay"
    )


# ---------------------------------------------------------------------------
# Acceptance command: the whole chain runs green on a fresh sqlite database
# ---------------------------------------------------------------------------

def test_alembic_upgrade_head_on_sqlite_is_guarded(tmp_path):
    db = tmp_path / "mig_sqlite_test.db"
    env = {
        **os.environ,
        "FD_OPEN_DATA_MCP_DATABASE_URL": f"sqlite:///{db}",
        "FD_OPEN_DATA_MCP_ALEMBIC_DIR": str(ALEMBIC_DIR),
    }
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr

    engine = create_engine(f"sqlite:///{db}")
    try:
        with engine.connect() as conn:
            version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
            tables = {
                row[0]
                for row in conn.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table'")
                )
            }
    finally:
        engine.dispose()
    assert version == "0007_legal_federation"
    # Guarded no-op: only the alembic ledger exists; no app tables were built.
    assert tables == {"alembic_version"}
