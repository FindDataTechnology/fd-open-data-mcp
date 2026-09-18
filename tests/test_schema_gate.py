"""Startup schema gate tests (db-schema-lifecycle tasks 5.1-5.3, 6.3).

Three layers:

- Pure unit tests of the comparison logic in ``evaluate_schema_gate``.
- SQLite exemption: the ``session`` fixture works without a ledger because the
  gate never runs for sqlite URLs (the baseline revision is PostgreSQL-only).
- PostgreSQL 14 integration tests against the local scratch server at
  127.0.0.1:55432 (skipped when unreachable): a database with no tables fails
  startup instead of creating them, a migrated database passes, a database
  older than the shipped chain refuses naming both revisions, and the bypass
  env var allows startup while warning loudly.
"""
from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

from fd_open_data_mcp.db import (
    Database,
    GateVerdict,
    SchemaGateError,
    _resolve_alembic_dir,
    evaluate_schema_gate,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_DIR = PROJECT_ROOT / "alembic"

PG_HOST = "127.0.0.1"
PG_PORT = 55432
PG_USER = "fdtest"
PG_BIN = Path("/opt/homebrew/opt/postgresql@14/bin")


def _pg_reachable() -> bool:
    try:
        with socket.create_connection((PG_HOST, PG_PORT), timeout=1.0):
            return True
    except OSError:
        return False


def _chain_head() -> str:
    """Current head of the shipped chain — assertions must not hardcode it."""
    from alembic.script import ScriptDirectory

    return ScriptDirectory(str(ALEMBIC_DIR)).get_current_head()


# --------------------------------------------------------------------------
# Pure gate logic
# --------------------------------------------------------------------------

CHAIN = ["0000_old", "0001_middle", "0002_newest"]


class TestEvaluateSchemaGate:
    def test_equal_to_head_passes(self):
        verdict, _ = evaluate_schema_gate(["0002_newest"], CHAIN)
        assert verdict is GateVerdict.CURRENT

    def test_head_of_single_revision_chain_passes(self):
        verdict, _ = evaluate_schema_gate(
            ["0001_schema_baseline"], ["0001_schema_baseline"]
        )
        assert verdict is GateVerdict.CURRENT

    def test_known_older_revision_is_rejected_naming_both(self):
        verdict, message = evaluate_schema_gate(["0001_middle"], CHAIN)
        assert verdict is GateVerdict.BEHIND
        assert "0001_middle" in message
        assert "0002_newest" in message

    def test_unknown_revision_is_allowed_as_rolling_window(self):
        verdict, message = evaluate_schema_gate(["9999_future"], CHAIN)
        assert verdict is GateVerdict.AHEAD_UNKNOWN
        assert "9999_future" in message
        assert "0002_newest" in message

    @pytest.mark.parametrize("db_revisions", [None, []])
    def test_missing_ledger_error_points_at_stamp_runbook(self, db_revisions):
        verdict, message = evaluate_schema_gate(db_revisions, CHAIN)
        assert verdict is GateVerdict.LEDGER_MISSING
        assert "alembic_version" in message
        assert "alembic stamp 0001_schema_baseline" in message


class TestAlembicDirResolution:
    def test_env_var_pointing_at_missing_dir_is_an_ops_error(self, monkeypatch, tmp_path):
        monkeypatch.setenv("FD_OPEN_DATA_MCP_ALEMBIC_DIR", str(tmp_path / "missing"))
        with pytest.raises(SchemaGateError, match="FD_OPEN_DATA_MCP_ALEMBIC_DIR"):
            _resolve_alembic_dir()

    def test_env_var_pointing_at_real_dir_wins(self, monkeypatch):
        monkeypatch.setenv("FD_OPEN_DATA_MCP_ALEMBIC_DIR", str(ALEMBIC_DIR))
        assert _resolve_alembic_dir() == ALEMBIC_DIR


# --------------------------------------------------------------------------
# SQLite exemption (conftest builds the schema; gate never runs)
# --------------------------------------------------------------------------

def test_sqlite_is_exempt_and_test_builder_creates_schema(session):
    # The session fixture works post-refactor: schema comes from the explicit
    # test builder, not from a connection-triggered create_all.
    engine = session.get_bind()
    names = set(inspect(engine).get_table_names())
    assert "concepts" in names
    # Gate skipped for sqlite: no ledger table was ever created or required.
    assert "alembic_version" not in names
    assert session.execute(text("SELECT count(*) FROM concepts")).scalar() == 0


# --------------------------------------------------------------------------
# PostgreSQL integration (local scratch server; skipped when unreachable)
# --------------------------------------------------------------------------

def _public_tables(database_url: str) -> set[str]:
    engine = create_engine(database_url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT tablename FROM pg_catalog.pg_tables "
                    "WHERE schemaname = 'public'"
                )
            ).fetchall()
        return {row[0] for row in rows}
    finally:
        engine.dispose()


def _write_fake_chain(root: Path) -> Path:
    """Two-revision chain (0000_old -> 0001_new_head) for ordering tests."""
    versions = root / "versions"
    versions.mkdir(parents=True)
    (versions / "0000_old.py").write_text(
        'revision = "0000_old"\ndown_revision = None\n\n\ndef upgrade():\n    pass\n',
        encoding="utf-8",
    )
    (versions / "0001_new_head.py").write_text(
        'revision = "0001_new_head"\ndown_revision = "0000_old"\n\n\ndef upgrade():\n    pass\n',
        encoding="utf-8",
    )
    return root


def _stamp_ledger(database_url: str, revision: str) -> None:
    engine = create_engine(database_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
            )
            conn.execute(
                text("INSERT INTO alembic_version (version_num) VALUES (:rev)"),
                {"rev": revision},
            )
    finally:
        engine.dispose()


@pytest.fixture
def pg_database_url():
    name = f"fdmcp_gate_test_{uuid.uuid4().hex[:10]}"
    subprocess.run(
        [str(PG_BIN / "createdb"), "-h", PG_HOST, "-p", str(PG_PORT), "-U", PG_USER, name],
        check=True,
        capture_output=True,
    )
    try:
        yield f"postgresql://{PG_USER}@{PG_HOST}:{PG_PORT}/{name}"
    finally:
        subprocess.run(
            [str(PG_BIN / "dropdb"), "--if-exists", "-h", PG_HOST, "-p", str(PG_PORT), "-U", PG_USER, name],
            check=False,
            capture_output=True,
        )


@pytest.mark.skipif(not _pg_reachable(), reason=f"no PostgreSQL at {PG_HOST}:{PG_PORT}")
class TestSchemaGateOnPostgreSQL:
    def test_missing_table_db_fails_startup_instead_of_creating(self, pg_database_url, monkeypatch):
        # Task 5.1: a database without tables must fail startup, not build them.
        monkeypatch.setenv("FD_OPEN_DATA_MCP_ALEMBIC_DIR", str(ALEMBIC_DIR))
        db = Database(pg_database_url)
        with pytest.raises(SchemaGateError) as excinfo:
            db.get_session()
        message = str(excinfo.value)
        assert "alembic_version" in message
        assert "alembic stamp 0001_schema_baseline" in message
        # Schema capture before/after the failed start is identical: empty.
        assert _public_tables(pg_database_url) == set()
        db.dispose()

    def test_migrated_db_passes_gate(self, pg_database_url, monkeypatch):
        monkeypatch.setenv("FD_OPEN_DATA_MCP_ALEMBIC_DIR", str(ALEMBIC_DIR))
        env = {
            **os.environ,
            "FD_OPEN_DATA_MCP_DATABASE_URL": pg_database_url,
            "FD_OPEN_DATA_MCP_ALEMBIC_DIR": str(ALEMBIC_DIR),
        }
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=PROJECT_ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        db = Database(pg_database_url)
        session = db.get_session()
        try:
            assert session.execute(text("SELECT version_num FROM alembic_version")).scalar() == _chain_head()
            # Migrated schema is usable by the application code.
            assert session.execute(text("SELECT count(*) FROM concepts")).scalar() == 0
        finally:
            session.close()
            db.dispose()

    def test_older_db_refuses_to_start_naming_both_revisions(self, pg_database_url, tmp_path, monkeypatch):
        fake_dir = _write_fake_chain(tmp_path / "alembic")
        monkeypatch.setenv("FD_OPEN_DATA_MCP_ALEMBIC_DIR", str(fake_dir))
        _stamp_ledger(pg_database_url, "0000_old")
        db = Database(pg_database_url)
        with pytest.raises(SchemaGateError) as excinfo:
            db.get_session()
        message = str(excinfo.value)
        assert "0000_old" in message
        assert "0001_new_head" in message
        db.dispose()

    def test_bypass_allows_ledgerless_db_and_warns_loudly(self, pg_database_url, monkeypatch, caplog):
        monkeypatch.setenv("FD_OPEN_DATA_MCP_ALEMBIC_DIR", str(ALEMBIC_DIR))
        monkeypatch.setenv("FD_OPEN_DATA_MCP_SCHEMA_GATE_BYPASS", "1")
        db = Database(pg_database_url)
        with caplog.at_level(logging.WARNING, logger="fd_open_data_mcp.db"):
            session = db.get_session()
            try:
                session.execute(text("SELECT 1"))
            finally:
                session.close()
        warnings = [r for r in caplog.records if "SCHEMA GATE BYPASSED" in r.message]
        assert warnings, "bypass must emit a warning"
        assert "none" in warnings[0].getMessage()  # db side: no ledger
        assert _chain_head() in warnings[0].getMessage()  # required side
        # Bypass is pass-through only: still no schema mutation.
        assert _public_tables(pg_database_url) == set()
        db.dispose()
