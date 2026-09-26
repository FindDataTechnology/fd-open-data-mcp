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

    Never raises: a mirror failure logs a warning and returns 0 so the
    policy-side close (the thing that matters) proceeds untouched.
    """
    try:
        status = _STATUS_MAP.get(run.status)
        if status is None:
            return 0  # still open, or a status that never executed
        if not run.job_ref:
            return 0  # born-closed refusal/launch-failure rows never executed
        sources = run_sources(run)
        if not sources:
            return 0  # source set not attributable — deliberate blank
        # exact rows when single-source; a zero-yield run wrote 0 everywhere
        rows_written = run.rows_new if (len(sources) == 1 or run.rows_new == 0) else None
        for src in sources:
            session.add(CrawlRun(
                source=src,
                kind="concept",
                status=status,
                started_at=run.started_at,
                finished_at=run.finished_at,
                rows_written=rows_written,
                error_head=(run.detail or "")[:255] if status == "failed" else None,
            ))
        session.flush()
        return len(sources)
    except Exception:  # noqa: BLE001 — mirror must not break the close path
        logger.warning("crawl_runs mirror failed for run %s",
                       getattr(run, "id", "?"), exc_info=True)
        return 0


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
