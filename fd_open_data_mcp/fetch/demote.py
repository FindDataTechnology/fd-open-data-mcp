"""Per-(cluster, function) reachability demotion (fix-silent-zero-yield-crawls D5).

The blocked unit is a HOST, not a datasource: ``akshare`` is simultaneously the
most reachable source (``datacenter.eastmoney.com`` all-OK) and the most
blocked (``push2*.eastmoney.com`` all ConnectionError) from the same cluster
egress. A demotion keyed on source/real_source cannot express that — which is
why the existing per-real_source circuit never opened for the 2026-08-22
outage. This module keys on ``(cluster_id, function_id)``, driven by observed
``fetch_log`` outcomes (no hand-maintained host denylist, which would rot the
first time an endpoint moves).

A function is demoted for a cluster when its last N consecutive fetch_log
rows FROM THAT CLUSTER are all errors (N = ``SCRAW_DEMOTE_CONSECUTIVE``,
default 5). Restore is automatic: because demoted entries are re-ordered to
the END of the failover chain (not removed), the occasional probe still goes
through them; a single ok row breaks the consecutive-failure streak and the
function returns to its configured rank.
"""
from __future__ import annotations

import logging
import os
from typing import Optional, Sequence

from sqlalchemy.orm import Session

from fd_open_data_mcp.models import FetchLog

logger = logging.getLogger(__name__)


def demote_threshold() -> int:
    """Consecutive failures before a (cluster, function) pair is demoted."""
    try:
        return max(1, int(os.environ.get("SCRAW_DEMOTE_CONSECUTIVE", "5")))
    except ValueError:
        return 5


def demoted_functions(
    session: Session,
    cluster_id: Optional[int],
    function_ids: Sequence[int],
    lookback: int = 500,
) -> set[int]:
    """The subset of ``function_ids`` demoted for this cluster.

    Reads the most recent ``lookback`` fetch_log rows per (cluster, function)
    and demotes when the last ``SCRAW_DEMOTE_CONSECUTIVE`` outcomes are ALL
    errors. Rows with no cluster_id (read()-path / ships-dark) never count:
    demotion is about a CLUSTER's egress, not about the endpoint in general.
    """
    if cluster_id is None or not function_ids:
        return set()
    n = demote_threshold()
    out: set[int] = set()
    for fid in set(function_ids):
        rows = (
            session.query(FetchLog.status)
            .filter(FetchLog.function_id == fid,
                    FetchLog.cluster_id == cluster_id)
            .order_by(FetchLog.id.desc())
            .limit(max(n, 1))
            .all()
        )
        if len(rows) >= n and all(status != "ok" for (status,) in rows):
            out.add(fid)
    return out


def reorder_chain(
    session: Session,
    cluster_id: Optional[int],
    chain: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Split a ranked-source chain into (healthy, demoted) preserving order.

    ``chain`` entries carry ``function_id`` (the planner's PlanSource dump).
    Demoted entries keep their relative order and are appended after the
    healthy ones — still reachable as probes (which is how they restore) but
    never preferred over a reachable alternative.
    """
    fids = [rs.get("function_id") for rs in chain if rs.get("function_id") is not None]
    if not fids:
        return chain, []
    bad = demoted_functions(session, cluster_id, fids)
    if not bad:
        return chain, []
    healthy = [rs for rs in chain if rs.get("function_id") not in bad]
    demoted = [rs for rs in chain if rs.get("function_id") in bad]
    return healthy, demoted
