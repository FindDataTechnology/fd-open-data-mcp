"""concept-platform-federation: mirror policy_runs closes into crawl_runs.

The concept line joins the platform as a federated member (unified-source-
onboarding): sources are registered in crawl_sources under their protocol
manifest names, and every run that ACTUALLY EXECUTED (has a job_ref) is
mirrored into crawl_runs (kind='concept') at its close site. Refusal rows
(guardrail / frozen-window / launch failures) are born closed with no job_ref
and are never mirrored — nothing executed.

Row-count attribution (spec: 报到行数按源可归属性分级):
- a policy pinning a single source (source_filter len 1) reports the exact
  run yield (rows_new) on that one row;
- a multi-source run splits one crawl_runs row per planned source (plan_json
  ranked_sources, deduped) with rows_written=NULL — except a zero-yield run
  (rows_new == 0), where 0 propagates exactly to every source;
- runs whose source set cannot be attributed (no plan_json, no single-source
  filter — direct scripts) are deliberately not mirrored.

Mirroring must never break the close path (crawl-run-telemetry: reporting
failure is swallowed with a local log trace).
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from fd_open_data_mcp.models import CrawlRun, CrawlSource, PolicyRun, Source

logger = logging.getLogger(__name__)

# policy_runs terminal statuses -> crawl_runs statuses. no_op / redundant /
# zero_yield all ran to completion: they report as success (rows carry the
# signal; the zero-yield alarm stays on the concept line's own KPI face).
_STATUS_MAP = {
    "success": "success",
    "redundant": "success",
    "zero_yield": "success",
    "no_op": "success",
    "failed": "failed",
    "cancelled": "cancelled",
}


def run_sources(run: PolicyRun) -> list[str]:
    """Attributable source names for a run, in stable deduped order.

    A single-source policy filter wins (exact attribution). Otherwise the
    planned source chain of every wanted concept (plan_json) is unioned —
    the planned set is the reconciler-side truth; actual fetch usage lives
    only in fetch_log/pod memory (per-source counting is a later enhancement).
    """
    filt = run.policy.source_filter if run.policy is not None else None
    if isinstance(filt, (list, tuple)) and len(filt) == 1 and filt[0]:
        return [str(filt[0])]
    if run.plan_json:
        names: list[str] = []
        for concept in run.plan_json.get("wanted_concepts", []) or []:
            for ps in concept.get("ranked_sources", []) or []:
                name = ps.get("source")
                if name and name not in names:
                    names.append(name)
        return names
    return []


def mirror_run_close(session: Session, run: PolicyRun) -> int:
    """Mirror one closed PolicyRun into crawl_runs. Returns rows added.

    Writes in a SIBLING session/transaction bound to the caller's engine: a
    failing crawl_runs insert (constraint, schema drift, outage) rolls back
    only the mirror's transaction and can never poison the caller's session —
    the 2026-09-27 incident showed a caught flush error still rolled back the
    reconciler's shared session and killed the whole tick. Returns 0 on any
    failure after a local log trace; the policy-side close proceeds untouched.

    rows_written mirrors the platform's existing convention: unknowns are 0
    (prod column is NOT NULL DEFAULT 0 — federation rows record 0 for "not
    measured"). Exact yield is reported only for single-source runs whose pod
    actually reported counters; a zero-yield run's 0 propagates to every row.
    """
    payload = None
    try:
        payload = _run_payload(run)
        if payload is None:
            return 0
        rows, sources = payload
        mirror_session = Session(bind=session.get_bind())
        try:
            for row in rows:
                mirror_session.add(CrawlRun(**row))
            mirror_session.commit()
        finally:
            mirror_session.close()
        return len(sources)
    except Exception:  # noqa: BLE001 — mirror must not break the close path
        logger.warning("crawl_runs mirror failed for run %s",
                       getattr(run, "id", "?"), exc_info=True)
        return 0


def _run_payload(run: PolicyRun) -> tuple[list[dict], list[str]] | None:
    """Extract the crawl_runs rows for a closed run, or None when the run
    must not be mirrored (still open / never executed / unattributable)."""
    status = _STATUS_MAP.get(run.status)
    if status is None:
        return None  # still open, or a status that never executed
    if not run.job_ref:
        return None  # born-closed refusal/launch-failure rows never executed
    sources = run_sources(run)
    if not sources:
        return None  # source set not attributable — deliberate blank
    # exact yield only when the pod reported counters on a single-source run;
    # unknown (never-reported / multi-source) records 0, the platform convention
    rows_written = run.rows_new if len(sources) == 1 and run.rows_new is not None else 0
    error_head = (run.detail or "")[:255] if status == "failed" else None
    rows = [{
        "source": src,
        "kind": "concept",
        "status": status,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "rows_written": rows_written,
        "error_head": error_head,
    } for src in sources]
    return rows, sources


def register_concept_sources(session: Session, site: str = "tencent") -> dict:
    """Register every catalog source (protocol manifest name) into crawl_sources.

    Insert-only idempotency: rows already present (e.g. a same-named content-repo
    source) are left untouched — their site/schedule/commit belong to their own
    registration path. Unlit (schedule=NULL) + enabled matches the federation
    semantics: visible in the inventory, nothing owed, no platform scheduling.
    """
    inserted = existing = 0
    for (name,) in session.query(Source.name).order_by(Source.name).all():
        if session.get(CrawlSource, name) is not None:
            existing += 1
            continue
        session.add(CrawlSource(source=name, site=site, schedule=None, enabled=True))
        inserted += 1
    session.commit()
    return {"inserted": inserted, "existing": existing, "site": site}
