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


# ─── Full-registry enumeration (indicator-caliber-unification D2) ────────────

#: Status filters for :func:`list_registry_entries`. Per the ADR-0002 caliber
#: vocabulary every row is "registered" and ``verified`` marks the narrower
#: verified subset — so ``status="registered"`` selects the NOT-verified
#: entries. Both predicates are NULL-safe: a row never yet reviewed
#: (``verified IS NULL``) is registered, never verified.
_STATUS_PREDICATES = {
    "verified": "verified IS TRUE",
    "registered": "verified IS NOT TRUE",
}

_LIST_COLUMNS = (
    "source_db, native_code, semantic_code, name_zh, name_en, "
    "unit, frequency, domain, verified"
)


def list_registry_entries(
    session: Session, status: str | None = None,
    limit: int = 500, offset: int = 0,
) -> list[dict[str, Any]]:
    """Page through the WHOLE registry — verified and not — as plain dicts.

    The parallel enumeration channel behind the tiered-browsing decision
    (ADR-0002): unlike :func:`read_verified_entries` — the catalog/search/
    read verified gate, which this function does NOT touch — it serves every
    registered entry and carries the authoritative ``verified`` flag on each
    row (always a real bool, never a driver int).

    Paging: rows are ordered by ``(source_db, native_code)``;
    ``limit`` clamps to [1, 1000] and ``offset`` floors at 0 (the
    ``list_concepts`` convention). The function slices ONE page — callers
    enumerate the full registry by advancing offset until a page comes back
    shorter than ``limit``.

    Args:
        status: ``None`` = all entries; ``"verified"`` = only verified;
            ``"registered"`` = only not-verified (NULL-safe — never-reviewed
            rows are registered). Anything else raises ``ValueError``.

    Fail-soft, same contract as :func:`read_verified_entries`: a missing
    table (or a missing-table error racing the existence check) yields
    ``[]`` plus a single log line — never an exception.
    """
    from fd_open_data_mcp.semantic.concepts import _clamp_limit

    if status is not None and status not in _STATUS_PREDICATES:
        raise ValueError(
            f"invalid status {status!r}: use None (all entries), "
            "'verified' or 'registered'"
        )
    limit = _clamp_limit(limit)
    offset = max(0, int(offset))
    where = f"WHERE {_STATUS_PREDICATES[status]}" if status else ""
    sql = text(
        f"SELECT {_LIST_COLUMNS} FROM {REGISTRY_TABLE} {where} "
        "ORDER BY source_db, native_code LIMIT :limit OFFSET :offset"
    )
    try:
        if not registry_table_exists(session):
            logger.info(
                "unified registry table %r not present; enumeration "
                "returns no entries", REGISTRY_TABLE,
            )
            return []
        rows = session.execute(sql, {"limit": limit, "offset": offset}).mappings().all()
    except (OperationalError, ProgrammingError) as exc:
        logger.info(
            "unified registry %r unreadable (%s); enumeration returns no "
            "entries", REGISTRY_TABLE, exc,
        )
        return []
    return [{**dict(row), "verified": bool(row["verified"])} for row in rows]
