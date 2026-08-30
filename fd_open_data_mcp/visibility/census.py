"""Per-store observation census (add-shard-aware-coverage).

Collects one ``data_census`` row per observation store: the local master
``semantic_observations`` (exact count + catalog size) and each shard foreign
server (catalog/stats probes over ``dblink`` riding the existing postgres_fdw
user mappings — no credentials needed).

HARD CONSTRAINT (daas-doc/RUNBOOK.md): never read shard fact tables. The 98M
shard's grouped scans OOM-kill its 4GB backend; shard figures here come only
from ``approximate_row_count()`` and ``timescaledb_information.chunks``
(catalog/stats), both sub-second. A failing probe records an error on that
store's row without aborting the others.
"""
from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

from fd_open_data_mcp.models import DataCensus

logger = logging.getLogger(__name__)

LOCAL_STORE = "gz_master"

# catalog-only: row estimate + chunk count + data time-axis upper bound.
# dollar-quoting keeps the shard-side literals intact through dblink.
_REMOTE_PROBE_SQL = """
SELECT approximate_row_count('semantic_observations') AS approx_rows,
       (SELECT count(*) FROM timescaledb_information.chunks
        WHERE hypertable_name = 'semantic_observations') AS chunks,
       (SELECT max(range_end)::text FROM timescaledb_information.chunks
        WHERE hypertable_name = 'semantic_observations') AS range_end
"""


def _dblink_probe(conn, server: str) -> dict:
    """Run the remote catalog probe on one shard. Raises on any failure
    (missing extension, unreachable shard, permission) — the caller records
    the error on the store's census row."""
    rows = conn.execute(text(
        f"SELECT * FROM dblink('{server}', $probe${_REMOTE_PROBE_SQL}$probe$) "
        "AS t(approx_rows bigint, chunks bigint, range_end text)"
    )).fetchone()
    return {"approx_rows": int(rows[0]), "chunks": int(rows[1]),
            "range_end": rows[2]}


def _shard_servers(conn) -> list[str]:
    """Foreign server names, discovered — not hard-coded (design D1)."""
    return [r[0] for r in conn.execute(text("SELECT srvname FROM pg_foreign_server")).fetchall()]


def _upsert(session: Session, store: str, kind: str, **fields) -> None:
    row = session.query(DataCensus).filter_by(store=store).first()
    if row is None:
        row = DataCensus(store=store, kind=kind)
        session.add(row)
    row.kind = kind
    row.sampled_at = dt.datetime.utcnow()
    for k, v in fields.items():
        setattr(row, k, v)


def refresh_census(session: Session, *, probe=None) -> dict:
    """Re-collect the census for every store. Read-only against shards.

    ``probe`` overrides the dblink probe (tests; design D5) — resolved at
    call time so monkeypatching ``census._dblink_probe`` also works. Returns
    a summary dict; per-store failures land in the store's census row as
    ``error``.
    """
    if probe is None:
        probe = _dblink_probe
    conn = session.connection()

    # local master: exact count + catalog size (a refresh action, not a page)
    local_error = None
    approx_rows = size_bytes = None
    try:
        approx_rows = conn.execute(text("SELECT count(*) FROM semantic_observations")).scalar()
        size_bytes = conn.execute(
            text("SELECT pg_total_relation_size('semantic_observations')")
        ).scalar()
    except Exception as e:  # noqa: BLE001
        local_error = str(e)
    _upsert(session, LOCAL_STORE, "local", approx_rows=approx_rows, exact=True,
            total_size_bytes=size_bytes, chunks=None, time_range_end=None,
            error=local_error)

    servers: list[str] = []
    try:
        servers = _shard_servers(conn)
    except Exception as e:  # noqa: BLE001 - e.g. SQLite tests: no pg catalog
        logger.debug("no foreign servers (%s)", e)

    results = {LOCAL_STORE: {"approx_rows": approx_rows, "error": local_error}}
    for srv in servers:
        error = None
        fields: dict = {}
        try:
            fields = probe(conn, srv)
        except Exception as e:  # noqa: BLE001 - record, keep the other stores
            error = _hint(e)
        _upsert(session, srv, "shard",
                approx_rows=fields.get("approx_rows"), exact=False,
                total_size_bytes=None, chunks=fields.get("chunks"),
                time_range_end=fields.get("range_end"), error=error)
        results[srv] = {"approx_rows": fields.get("approx_rows"), "error": error}

    session.commit()
    return {"stores": results, "sampled_at": dt.datetime.utcnow().isoformat()}


def _hint(e: Exception) -> str:
    """Name the fix when the failure is the missing dblink extension."""
    msg = str(e)
    if "dblink" in msg.lower() and ("does not exist" in msg.lower()
                                    or "no function matches" in msg.lower()):
        return (f"probe failed: {msg}. Run CREATE EXTENSION dblink on the "
                "master as superuser (see add-shard-aware-coverage design D1).")
    return f"probe failed: {msg}"


def latest_census(session: Session) -> list[dict]:
    """All stored census rows (pages read these; no collection here)."""
    return [r.toDict() for r in session.query(DataCensus).order_by(DataCensus.kind).all()]
