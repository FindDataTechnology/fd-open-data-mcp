"""Pytest fixtures: each test gets a fresh isolated SQLite DB."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Add project root to Python path for local imports (for smoke tests)
project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from fd_open_data_mcp import db as dbmod
from fd_open_data_mcp.models import Base


@pytest.fixture
def session(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("FD_OPEN_DATA_MCP_DATABASE_URL", f"sqlite:///{db_path}")
    dbmod.reset_database()
    db = dbmod.get_database()
    # Test-only schema builder: connections no longer create tables (schema
    # gate), and the baseline revision is PostgreSQL-only DDL, so sqlite tests
    # build the schema explicitly from the models. create_all is equivalent to
    # the baseline here - schema_baseline.sql is its verified 2026-09-18
    # snapshot; the full migration chain is exercised on PostgreSQL.
    Base.metadata.create_all(db.engine)
    s = db.get_session()
    yield s
    s.close()
    dbmod.reset_database()
