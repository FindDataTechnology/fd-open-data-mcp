"""Suppression of permanently-failing ``(concept, function)`` dispatch paths.

Permanent failures are **intrinsic to the request** — the installed library has
no such callable, the signature does not accept the built params. Nothing about
the exit, the retry, the cooldown or the cluster changes that, which is why a
permanent path is *excluded* rather than demoted, and why suppression needs no
cluster scope: unlike a ban, a missing callable is missing from every egress.

The evidence is the same ``fetch_log`` the demotion path reads, so there is no
hand-maintained denylist to rot. A pair is suppressed when its most recent
``N`` recorded outcomes are ALL permanent (N = ``SCRAW_SUPPRESS_MIN``,
default 3) — consecutive by recency, so one ordinary outcome clears it without
anyone intervening.

Clearing is deliberate as well as automatic: :func:`clear_suppression` records
an operator marker row, which breaks the run of permanent outcomes. Nothing is
deleted, so a wrong suppression is undone by data, not by a code change.

Cost note (task 7.5): :func:`suppressed_paths` is asked about the pairs already
in a chain, so it is bounded by the chain length and served by the
``fetch_log(function_id)`` / ``(concept_id)`` indexes. Callers compute it ONCE
per run and pass the set down; it is not a per-cell query.
"""
from __future__ import annotations

import logging
import os
from typing import Iterable, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

SUPPRESS_MIN_ENV = "SCRAW_SUPPRESS_MIN"
DEFAULT_SUPPRESS_MIN = 3

# The classification a permanent failure carries (spec concept-fetch). Kept as a
# literal so this module does not import the proxy package for one string.
PERMANENT = "permanent"
# Written by clear_suppression; deliberately not a failure class, so it breaks
# a run of permanent outcomes without pretending a value was fetched.
CLEARED = "cleared"


def suppress_threshold() -> int:
    """Consecutive permanent outcomes before a path is suppressed."""
    try:
        return max(1, int(os.environ.get(SUPPRESS_MIN_ENV, DEFAULT_SUPPRESS_MIN)))
    except ValueError:
        return DEFAULT_SUPPRESS_MIN


def _is_suppressed_pair(
    session: Session, concept_id: int, function_id: int, n: int,
) -> bool:
    rows = session.execute(
        text(
            "SELECT classification FROM fetch_log "
            "WHERE concept_id = :cid AND function_id = :fid "
            "ORDER BY id DESC LIMIT :n"
        ),
        {"cid": concept_id, "fid": function_id, "n": n},
    ).fetchall()
    if len(rows) < n:
        return False
    return all((r[0] or "") == PERMANENT for r in rows)


def suppressed_paths(
    session: Session,
    pairs: Iterable[tuple[int, int]],
    threshold: Optional[int] = None,
) -> set[tuple[int, int]]:
    """The subset of ``pairs`` suppressed as permanently failing.

    ``pairs`` is ``(concept_id, function_id)``. Bounded by the input, so a
    caller passes the pairs from the chain it is about to attempt.
    """
    n = threshold if threshold is not None else suppress_threshold()
    out: set[tuple[int, int]] = set()
    for concept_id, function_id in set(pairs):
        if concept_id is None or function_id is None:
            continue
        if _is_suppressed_pair(session, concept_id, function_id, n):
            out.add((concept_id, function_id))
    return out


def clear_suppression(session: Session, concept_id: int, function_id: int) -> bool:
    """Record that an operator cleared a suppressed path.

    Writes a marker row rather than deleting evidence: the pair's most recent
    outcomes are then no longer all permanent, so it becomes dispatch-eligible
    again and the clearing itself is auditable.
    """
    from fd_open_data_mcp.models import FetchLog
    from datetime import datetime, timezone

    if concept_id is None or function_id is None:
        return False
    session.add(FetchLog(
        source="operator", concept_id=concept_id, entity_type=None, entity_id=None,
        latency_ms=0, status="error", detail="suppression cleared by operator",
        proxy_id=None, classification=CLEARED, real_source=None,
        function_id=function_id, cluster_id=None, timestamp=datetime.now(timezone.utc),
    ))
    session.commit()
    return True


def report(session: Session, threshold: Optional[int] = None) -> dict:
    """Every suppressed path, with the evidence that produced it. Read-only."""
    n = threshold if threshold is not None else suppress_threshold()
    rows = session.execute(
        text(
            "SELECT concept_id, function_id, "
            "       count(*) AS outcomes, max(timestamp) AS last_seen, "
            "       max(detail) AS last_detail "
            "FROM fetch_log "
            "WHERE concept_id IS NOT NULL AND function_id IS NOT NULL "
            "  AND classification = :perm "
            "GROUP BY concept_id, function_id "
            "HAVING count(*) >= :n "
            "ORDER BY outcomes DESC"
        ),
        {"perm": PERMANENT, "n": n},
    ).fetchall()

    suppressed = []
    for concept_id, function_id, outcomes, last_seen, last_detail in rows:
        if not _is_suppressed_pair(session, concept_id, function_id, n):
            continue        # an ordinary outcome has since broken the run
        suppressed.append({
            "concept_id": concept_id, "function_id": function_id,
            "permanent_outcomes": outcomes,
            # SQLite hands back a string for max(timestamp); Postgres a datetime.
            "last_seen": (last_seen.isoformat() if hasattr(last_seen, "isoformat")
                          else (str(last_seen) if last_seen else None)),
            "last_detail": (last_detail or "")[:200],
        })
    return {"threshold": n, "suppressed": suppressed, "count": len(suppressed)}
