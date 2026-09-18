"""Database module for fd-open-data-mcp.

SQLAlchemy engine + session factory. Reads ``FD_OPEN_DATA_MCP_DATABASE_URL``
from the environment (defaults to SQLite at ``metadata/daas.db`` next to the
package). Enables WAL + foreign_keys for SQLite.

Connections never mutate schema. Instead of creating tables, ``init_db`` runs a
startup schema gate: the revision recorded in the database's ``alembic_version``
ledger must be at least the head of the alembic chain shipped alongside the
code (``database >= required``; during a rolling update the old code briefly
faces a newer schema and must still start). A database older than the code
refuses to start with a RuntimeError naming both revisions; a database with no
ledger at all fails with an error pointing at the adoption runbook. SQLite URLs
are exempt (see ``_enforce_schema_gate``), and
``FD_OPEN_DATA_MCP_SCHEMA_GATE_BYPASS=1`` is the single, loud escape hatch.

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

import enum
import logging
import os
from pathlib import Path
from typing import Optional, Sequence

# Load environment variables from .env files
try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=".env.local", override=True)
    load_dotenv(dotenv_path=".env", override=False)
except ImportError:
    # python-dotenv not installed, skip loading
    pass

from sqlalchemy import Engine, create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)

_DEFAULT_DB_DIR = Path(__file__).resolve().parent / "metadata"
_DEFAULT_DB_PATH = _DEFAULT_DB_DIR / "daas.db"

_ALEMBIC_DIR_ENV = "FD_OPEN_DATA_MCP_ALEMBIC_DIR"
_BYPASS_ENV = "FD_OPEN_DATA_MCP_SCHEMA_GATE_BYPASS"
_VERSION_TABLE = "alembic_version"

_STAMP_GUIDANCE = (
    "this is a pre-adoption database (no alembic_version ledger): see the "
    "adoption runbook - either run the migrations ('alembic upgrade head') or, "
    "for an existing schema already verified equal to the baseline, record it "
    "with 'alembic stamp 0001_schema_baseline'"
)


class SchemaGateError(RuntimeError):
    """Raised when the database cannot be shown to match the shipped schema."""


class GateVerdict(enum.Enum):
    """Outcome of comparing the database's ledger to the shipped chain."""

    CURRENT = "current"  # database at the shipped head
    BEHIND = "behind"  # database older than the code requires -> refuse
    AHEAD_UNKNOWN = "ahead-unknown"  # revision from newer code -> allow + warn
    LEDGER_MISSING = "ledger-missing"  # no alembic_version table/rows -> refuse


def evaluate_schema_gate(
    db_revisions: Optional[Sequence[str]],
    chain: Sequence[str],
) -> tuple[GateVerdict, str]:
    """Pure gate comparison: recorded revisions vs the shipped revision chain.

    ``chain`` is the alembic revision chain in application order (oldest
    first); its last element is the head this image requires. ``db_revisions``
    is what the database's ``alembic_version`` table records (``None`` or empty
    when there is no ledger). Only ``database >= required`` is enforced: a
    revision absent from the shipped chain is assumed to come from newer code
    still in its rolling window and is allowed with a warning.
    """
    head = chain[-1]
    if not db_revisions:
        return GateVerdict.LEDGER_MISSING, f"database has no {_VERSION_TABLE} ledger; {_STAMP_GUIDANCE}"
    unknown = [rev for rev in db_revisions if rev not in chain]
    if unknown:
        return GateVerdict.AHEAD_UNKNOWN, (
            f"database revision(s) {sorted(unknown)} are not in the alembic chain "
            f"shipped with this image (head {head!r}); treating them as a newer "
            "deployment in a rolling window and allowing startup"
        )
    positions = [chain.index(rev) for rev in db_revisions]
    oldest = chain[min(positions)]
    if min(positions) < len(chain) - 1:
        return GateVerdict.BEHIND, (
            f"database schema revision {oldest!r} is older than this image "
            f"requires (head {head!r}); run the migrations ('alembic upgrade "
            "head') before starting. To start anyway against a possibly "
            f"incompatible schema, set {_BYPASS_ENV}=1 (loud, not recommended)"
        )
    return GateVerdict.CURRENT, ""


def _resolve_alembic_dir() -> Path:
    """Locate the alembic script directory shipped with this code.

    Resolution order: ``FD_OPEN_DATA_MCP_ALEMBIC_DIR`` env var, ``$PWD/alembic``
    (running from a source checkout), then the package-relative ``alembic/``
    (source-tree layout). A missing directory is an operations problem, not
    something to silently skip.
    """
    env_dir = os.environ.get(_ALEMBIC_DIR_ENV)
    if env_dir:
        path = Path(env_dir)
        if not path.is_dir():
            raise SchemaGateError(
                f"{_ALEMBIC_DIR_ENV}={env_dir!r} is not a directory; cannot "
                "determine the required schema revision"
            )
        return path
    for candidate in (
        Path.cwd() / "alembic",
        Path(__file__).resolve().parents[2] / "alembic",
    ):
        if candidate.is_dir():
            return candidate
    raise SchemaGateError(
        "no alembic script directory found (tried $PWD/alembic and the "
        "package-relative alembic/); the image does not ship the alembic "
        "scripts needed to determine the required schema revision - set "
        f"{_ALEMBIC_DIR_ENV} to the directory containing versions/ or fix the "
        "image packaging"
    )


def _load_revision_chain(alembic_dir: Path) -> list[str]:
    """Return the shipped revision chain in application order (oldest first)."""
    try:
        from alembic.script import ScriptDirectory
    except ImportError as exc:  # pragma: no cover - alembic is a hard dependency
        raise SchemaGateError(
            "the alembic package is not installed; cannot determine the "
            "required schema revision"
        ) from exc
    script_dir = ScriptDirectory(str(alembic_dir))
    head = script_dir.get_current_head()
    if head is None:
        raise SchemaGateError(
            f"alembic script directory {alembic_dir} contains no revisions; "
            "the image is broken"
        )
    # walk_revisions() yields newest first; reverse for oldest first.
    chain = [rev.revision for rev in script_dir.walk_revisions()]
    chain.reverse()
    return chain


def _read_db_revisions(engine: Engine) -> Optional[list[str]]:
    """Read the revisions recorded in the database's alembic_version ledger.

    Returns ``None`` when the ledger table does not exist, and an empty list
    when it exists but records nothing; the gate treats both as pre-adoption.
    """
    if not inspect(engine).has_table(_VERSION_TABLE):
        return None
    with engine.connect() as conn:
        rows = conn.execute(text(f"SELECT version_num FROM {_VERSION_TABLE}")).fetchall()
    return [row[0] for row in rows]


def _safe_db_revisions(engine: Engine) -> str:
    """Best-effort revision report for the bypass warning; never raises."""
    try:
        revisions = _read_db_revisions(engine)
    except Exception as exc:  # noqa: BLE001 - bypass must never fail closed
        return f"<unreadable: {exc}>"
    if not revisions:
        return f"none ({_VERSION_TABLE} ledger missing or empty)"
    return ", ".join(revisions)


def _safe_required_head() -> str:
    """Best-effort required-head report for the bypass warning; never raises."""
    try:
        return _load_revision_chain(_resolve_alembic_dir())[-1]
    except Exception as exc:  # noqa: BLE001 - bypass must never fail closed
        return f"<unavailable: {exc}>"


def _enforce_schema_gate(engine: Engine, database_url: str) -> None:
    """Refuse to run against a schema older than the shipped alembic head.

    SQLite URLs are exempt: the baseline revision executes PostgreSQL-only DDL
    (pg_dump output), so sqlite databases - tests and local use only,
    production is PostgreSQL - cannot run the migration chain and are never
    gated. The bypass environment variable is the only other way past the
    gate, and it warns loudly.
    """
    if database_url.startswith("sqlite"):
        logger.debug("schema gate skipped for sqlite database: %s", database_url)
        return
    if os.environ.get(_BYPASS_ENV) == "1":
        logger.warning(
            "SCHEMA GATE BYPASSED via %s=1: starting anyway against a database "
            "that may not match the code. database revision(s): %s; required "
            "revision: %s",
            _BYPASS_ENV,
            _safe_db_revisions(engine),
            _safe_required_head(),
        )
        return
    chain = _load_revision_chain(_resolve_alembic_dir())
    verdict, message = evaluate_schema_gate(_read_db_revisions(engine), chain)
    if verdict is GateVerdict.AHEAD_UNKNOWN:
        logger.warning("schema gate: %s", message)
        return
    if verdict in (GateVerdict.BEHIND, GateVerdict.LEDGER_MISSING):
        raise SchemaGateError(message)
    logger.info("schema gate passed: database at required revision %r", chain[-1])


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
        _enforce_schema_gate(self._engine, self._database_url)
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
