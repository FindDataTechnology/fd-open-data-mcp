"""Read the unified indicator registry (``registry_entries``) — fail-soft.

The ``registry_entries`` table is owned by the unified-indicator-registry
pipeline and lives in the same ``fd_open_data`` database this service already
uses (physically on the production PostgreSQL master). This service only
READS it, with raw SQL through the existing session/engine:

  - no ORM model is declared (registry rows stay plain dicts), and
  - no migration is added and the alembic head is not bumped — the schema
    gate in ``db/__init__.py`` stays exactly as it is.

Fail-soft contract: when the table is missing (local SQLite dev databases,
or a PostgreSQL instance that has not run the registry DDL) the reader
returns ``[]`` after exactly one log line and never raises — the public
catalog then behaves exactly as before (native concepts only).
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import inspect, text
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

REGISTRY_TABLE = "registry_entries"

# Verified-only, ordered by the catalog merge key (domain, semantic_code)
# with native_code as a final tiebreaker so paging over the merged catalog
# is deterministic even where the unique semantic_code index is not enforced
# (the SQLite test fixture).
_SELECT_VERIFIED_SQL = text(
    "SELECT source_db, source_table, source_column, native_code, "
    "semantic_code, name_zh, name_en, unit, frequency, domain "
    f"FROM {REGISTRY_TABLE} "
    "WHERE verified "
    "ORDER BY domain, semantic_code, native_code"
)


def registry_table_exists(session: Session) -> bool:
    """True when the registry table exists on the session's engine."""
    return inspect(session.get_bind()).has_table(REGISTRY_TABLE)


def read_verified_entries(session: Session) -> list[dict[str, Any]]:
    """Verified registry entries as lightweight dicts (raw column values).

    Fail-soft: a missing table (or a missing-table error racing the
    existence check) yields ``[]`` plus a single log line — never an
    exception, so catalog callers keep their pre-registry behavior.
    """
    try:
        if not registry_table_exists(session):
            logger.info(
                "unified registry table %r not present; catalog serves "
                "native concepts only", REGISTRY_TABLE,
            )
            return []
        rows = session.execute(_SELECT_VERIFIED_SQL).mappings().all()
    except (OperationalError, ProgrammingError) as exc:
        logger.info(
            "unified registry %r unreadable (%s); catalog serves native "
            "concepts only", REGISTRY_TABLE, exc,
        )
        return []
    return [dict(row) for row in rows]
