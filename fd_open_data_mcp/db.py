"""Database module for fd-open-data-mcp.

SQLAlchemy engine + session factory. Reads ``FD_OPEN_DATA_MCP_DATABASE_URL``
from the environment (defaults to SQLite at ``metadata/daas.db`` next to the
package). Enables WAL + foreign_keys for SQLite. Auto-creates all tables on
first use.

Usage:
    from fd_open_data_mcp.db import get_database
    db = get_database()
    session = db.get_session()
    try:
        ...
        session.commit()
    finally:
        session.close()
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from fd_open_data_mcp.models import Base

logger = logging.getLogger(__name__)

_DEFAULT_DB_DIR = Path(__file__).resolve().parent / "metadata"
_DEFAULT_DB_PATH = _DEFAULT_DB_DIR / "daas.db"


class Database:
    """SQLAlchemy engine + session factory (singleton via get_database)."""

    def __init__(self, database_url: Optional[str] = None):
        if database_url is None:
            database_url = os.environ.get(
                "FD_OPEN_DATA_MCP_DATABASE_URL",
                f"sqlite:///{_DEFAULT_DB_PATH}",
            )
        self._database_url = database_url
        self._engine: Optional[Engine] = None
        self._session_factory: Optional[sessionmaker] = None

    @property
    def database_url(self) -> str:
        return self._database_url

    @property
    def engine(self) -> Engine:
        if self._engine is None:
            self.init_db()
        assert self._engine is not None
        return self._engine

    def get_session(self) -> Session:
        if self._session_factory is None:
            self.init_db()
        assert self._session_factory is not None
        return self._session_factory()

    def init_db(self) -> None:
        if self._database_url.startswith("sqlite"):
            db_path = self._database_url.replace("sqlite:///", "", 1)
            if db_path:
                Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        self._engine = create_engine(
            self._database_url,
            echo=False,
            connect_args=(
                {"check_same_thread": False}
                if self._database_url.startswith("sqlite")
                else {}
            ),
        )

        if self._database_url.startswith("sqlite"):
            @event.listens_for(self._engine, "connect")
            def _set_sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()

        self._session_factory = sessionmaker(bind=self._engine)
        Base.metadata.create_all(self._engine)
        logger.info("Database initialized: %s", self._database_url)

    def dispose(self) -> None:
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None
            self._session_factory = None


_database: Optional[Database] = None


def get_database(database_url: Optional[str] = None) -> Database:
    """Get or create the singleton Database instance."""
    global _database
    if _database is None:
        _database = Database(database_url)
        _database.init_db()
    return _database


def reset_database() -> None:
    """Dispose and reset the singleton. Useful for testing."""
    global _database
    if _database is not None:
        _database.dispose()
    _database = None
