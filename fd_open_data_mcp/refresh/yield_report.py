"""Pod-side yield reporting for direct-executor scripts (fix-silent-zero-yield-
crawls D1/D2).

The Scrapy pipeline reports its counters on every flush via
``scraw_fd_open_data_mcp.db.report_yield``. Direct-script pods get the SAME
``SCRAW_JOB_REF`` env injected by ``MultiClusterLauncher.launch``, and use this
module to update their run row — so a direct run's outcome is classified by
the same D3 table (counters absent + job succeeded => ``zero_yield``) instead
of silently reading as success.

Semantics note: direct scripts upsert with ``ON CONFLICT DO UPDATE`` (latest
value wins), so ``new`` here counts rows WRITTEN, not rows that were absent
before — a direct run that lands data reports ``rows_new > 0`` and closes
``success``, which is the intent (yield = data landed).
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def report_run_yield(attempted: int, new: int, job_ref: str | None = None) -> None:
    """Accumulate ``(attempted, new)`` onto the run identified by SCRAW_JOB_REF.

    Never raises: an accounting failure must not fail an ingest that already
    landed its rows (mirrors the fetch-instrumentation contract).
    """
    job_ref = job_ref or os.environ.get("SCRAW_JOB_REF")
    if not job_ref or (attempted <= 0 and new <= 0):
        return
    try:
        from sqlalchemy import text

        from fd_open_data_mcp.db import get_database

        engine = get_database().engine
        with engine.begin() as conn:
            conn.execute(
                text("""
                    UPDATE policy_runs
                    SET rows_attempted = COALESCE(rows_attempted, 0) + :attempted,
                        rows_new       = COALESCE(rows_new, 0) + :new
                    WHERE job_ref = :job_ref
                """),
                {"attempted": attempted, "new": new, "job_ref": job_ref},
            )
    except Exception:  # noqa: BLE001 - never let accounting break the ingest
        logger.warning("yield report failed (job_ref=%s)", job_ref, exc_info=True)
