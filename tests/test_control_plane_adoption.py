r"""Revision 0006_control_plane_adoption — guarded-adoption contract tests.

The revision adopts the 15 externally-bootstrapped tables (proposal
schema-drift-closure). Three of them are owned by earlier revisions
(scopes -> 0004, registry_entries + registry_indicator_embeddings -> 0003);
this revision creates the remaining twelve. The contract that matters most
is the **guarded no-op**: every statement is create-if-absent, so a database
that already carries the production shapes (or a database built by this very
revision) re-runs `upgrade` with zero effect — production rolls through the
migrate stage untouched, while a fresh database ends up with the complete
schema from the chain alone.

Pinned the same two ways as the 0003 contract tests: bound to a real SQLite
connection nothing must execute (dialect guard), and against a recording
fake-PG bind the exact guarded statements and their FK-safe ordering must
appear. The live-shape equivalence itself (DDL == `\d` of the authoritative
database) is the reports/adoption-diff.md reconciliation, re-verified
end-to-end by the CI sequence (upgrade -> alembic check) on pgvector.
"""
from __future__ import annotations

import contextlib
import importlib.util
import types
from pathlib import Path

import pytest
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_DIR = PROJECT_ROOT / "alembic"
REVISION_PATH = ALEMBIC_DIR / "versions" / "0006_control_plane_adoption.py"

ADOPTED_BY_0006 = (
    "crawl_sites",
    "crawl_sources",
    "crawl_runs",
    "crawl_items",
    "crawl_identities",
    "crawl_identity_events",
    "crawl_login_stations",
    "discoveries",
    "candidates",
    "analyses",
    "manifests",
    "pending_runs",
)
ADOPTED_BY_EARLIER_REVISIONS = (
    "scopes",                              # 0004, shape-equal
    "registry_entries",                    # 0003, guarded section
    "registry_indicator_embeddings",       # 0003, DDL contract
)
ADOPTED_INDEXES = (
    "crawl_runs_source_idx",
    "crawl_identities_pool_idx",
    "crawl_identities_lease_idx",
    "crawl_identity_events_identity_idx",
    "pending_runs_site_idx",
)


def _load_revision():
    spec = importlib.util.spec_from_file_location("mig_0006", REVISION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mig():
    return _load_revision()


def test_chain_head_and_wiring():
    script = ScriptDirectory(str(ALEMBIC_DIR))
    assert script.get_current_head() == "0009_source_schedule_tz"
    assert script.get_revision("0006_control_plane_adoption").down_revision == (
        "0005_concept_coverage"
    )


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
    broken = types.SimpleNamespace(
        dialect=types.SimpleNamespace(name="mysql"),
        exec_driver_sql=lambda *_: pytest.fail("guard must return before any SQL"),
    )
    fake_op = types.SimpleNamespace(get_bind=lambda: broken)
    original = mig.op
    mig.op = fake_op
    try:
        mig.upgrade()  # must simply return
        mig.downgrade()
    finally:
        mig.op = original


# ---------------------------------------------------------------------------
# Fake PG bind: the guarded statements the revision must emit
# ---------------------------------------------------------------------------

class _Recorder:
    def __init__(self):
        self.dialect = types.SimpleNamespace(name="postgresql")
        self.statements = []

    def exec_driver_sql(self, sql):
        self.statements.append(sql)


def _run_on_fake_pg(mig, fn="upgrade"):
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
        getattr(mig, fn)()
    finally:
        mig.op = original
    return bind


def _create_tables(bind):
    return [s for s in bind.statements if s.startswith("CREATE TABLE")]


def _create_indexes(bind):
    return [s for s in bind.statements if s.startswith("CREATE INDEX")]


def test_creates_exactly_the_twelve_0006_owned_tables(mig):
    bind = _run_on_fake_pg(mig)
    created = _create_tables(bind)
    assert len(created) == len(ADOPTED_BY_0006)
    for table in ADOPTED_BY_0006:
        assert any(
            s.startswith(f"CREATE TABLE IF NOT EXISTS {table} ") or
            s.startswith(f"CREATE TABLE IF NOT EXISTS {table}\n")
            for s in created
        ), table
    # The other three adopted tables belong to earlier revisions; re-emitting
    # their DDL here would create a second shape source.
    joined = "\n".join(bind.statements)
    for table in ADOPTED_BY_EARLIER_REVISIONS:
        assert f"CREATE TABLE IF NOT EXISTS {table}" not in joined, table


def test_every_statement_is_guarded_no_op_on_bootstrapped_db(mig):
    """The adoption no-op contract: on a database that already has the
    production shapes (i.e. production itself), upgrade() must execute only
    guarded statements — each one skips — so the migrate stage is a verified
    no-op (spec scenario 'Production migrates as a no-op')."""
    bind = _run_on_fake_pg(mig)
    assert bind.statements, "upgrade must emit its guarded statements"
    for s in bind.statements:
        stripped = s.lstrip()
        assert stripped.startswith("CREATE TABLE IF NOT EXISTS ") or \
            stripped.startswith("CREATE INDEX IF NOT EXISTS "), s
    # Re-running is semantically identical: same guarded statements, no
    # ALTER/DROP anywhere in the upgrade path.
    again = _run_on_fake_pg(mig)
    assert again.statements == bind.statements


def test_fk_dependency_order(mig):
    bind = _run_on_fake_pg(mig)
    order = {}
    for table in ADOPTED_BY_0006:
        stmt = next(
            s for s in bind.statements
            if s.startswith(f"CREATE TABLE IF NOT EXISTS {table}")
        )
        order[table] = bind.statements.index(stmt)
    for before, after in (
        ("crawl_sites", "crawl_sources"),
        ("crawl_sites", "pending_runs"),
        ("crawl_runs", "crawl_items"),
        ("crawl_identities", "crawl_identity_events"),
        ("crawl_identities", "crawl_login_stations"),
        ("discoveries", "candidates"),
        ("candidates", "analyses"),
        ("discoveries", "analyses"),
        ("discoveries", "manifests"),
        ("analyses", "manifests"),
    ):
        assert order[before] < order[after], (before, after)
    # an index follows its table
    for idx, table in (
        ("crawl_runs_source_idx", "crawl_runs"),
        ("crawl_identities_pool_idx", "crawl_identities"),
        ("crawl_identities_lease_idx", "crawl_identities"),
        ("crawl_identity_events_identity_idx", "crawl_identity_events"),
        ("pending_runs_site_idx", "pending_runs"),
    ):
        stmt = next(s for s in bind.statements if idx in s)
        assert bind.statements.index(stmt) > order[table], (idx, table)


def test_production_shape_details(mig):
    """Spot-pin the reconciliation rulings that autogen would never have
    produced on its own (reports/adoption-diff.md)."""
    bind = _run_on_fake_pg(mig)
    joined = "\n".join(bind.statements)
    for fragment in (
        # PK name unified to the deployed one (model said pk_crawl_items)
        "CONSTRAINT crawl_items_pkey PRIMARY KEY (run_id, idx)",
        # crawl_items FK restored (model had none)
        "REFERENCES crawl_runs(id) ON DELETE CASCADE",
        # deployed constraint names, not the model's originals
        "CONSTRAINT crawl_identities_source_account_alias_key",
        "CONSTRAINT crawl_identities_status_check",
        "CONSTRAINT crawl_identity_events_kind_check",
        "CONSTRAINT crawl_login_stations_status_check",
        "CONSTRAINT pending_runs_status_check",
        # deployed defaults the models had drifted from
        "requested_by TEXT NOT NULL DEFAULT 'console'",
        "max_attempts INTEGER NOT NULL DEFAULT 2",
        "site TEXT NOT NULL DEFAULT 'tencent'",
        # partial index
        "WHERE lease_token IS NOT NULL",
        # DESC in the deployed composite indexes
        "ON crawl_runs (source, created_at DESC)",
        "ON crawl_identity_events (identity_id, created_at DESC)",
        # NOT NULL columns production enforces
        "started_at TIMESTAMPTZ NOT NULL",
        "manifest_yaml TEXT NOT NULL",
        "page_url TEXT NOT NULL",
        "query_type VARCHAR(16) NOT NULL",
        "url TEXT NOT NULL",
    ):
        assert fragment in joined, fragment
    # no index that production lacks may be created (that would alter prod)
    for absent in (
        "ix_crawl_runs_status", "ix_crawl_runs_source", "ix_crawl_sources_site",
        "ix_pending_runs_source", "ix_pending_runs_site\n", "ix_pending_runs_status",
        "ix_analyses_status", "ix_discoveries_status", "ix_manifests_status",
        "idx_login_stations_identity", "ix_scopes_name",
    ):
        assert absent not in joined, absent


def test_downgrade_drops_only_the_twelve_in_reverse_order(mig):
    bind = _run_on_fake_pg(mig, fn="downgrade")
    drops = [s for s in bind.statements if s.startswith("DROP TABLE")]
    assert sorted(drops) == sorted(
        f"DROP TABLE IF EXISTS {t}" for t in ADOPTED_BY_0006
    )
    position = {s.split()[-1]: i for i, s in enumerate(drops)}
    # referencing tables drop before their FK targets
    for before, after in (
        ("crawl_sources", "crawl_sites"),
        ("pending_runs", "crawl_sites"),
        ("crawl_items", "crawl_runs"),
        ("crawl_identity_events", "crawl_identities"),
        ("crawl_login_stations", "crawl_identities"),
        ("candidates", "discoveries"),
        ("analyses", "discoveries"),
        ("manifests", "discoveries"),
        ("manifests", "analyses"),
    ):
        assert position[before] < position[after], (before, after)
    joined = "\n".join(bind.statements)
    for untouched in ("scopes", "registry_entries", "registry_indicator_embeddings"):
        assert untouched not in joined, untouched
    assert "CASCADE" not in joined, (
        "downgrade must not cascade — where out-of-band dependents exist it "
        "should fail loudly, not silently drop live history"
    )
