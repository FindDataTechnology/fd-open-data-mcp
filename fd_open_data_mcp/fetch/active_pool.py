"""Price active pool (design D5, capability ``price-active-pool``).

The pool answers "which symbols are worth pre-warming". Gap filling serves any
symbol a caller asks for, but a cold symbol pays an upstream round trip on its
first request; pre-warming moves that cost off the request path.

Two sources, and the distinction is recorded rather than flattened into a
boolean because the two behave differently over time:

- ``config`` — an operator named it. It stays until removed.
- ``frequency`` — it was among the most-requested symbols recently. It expires
  as soon as it falls out of the top N.

The frequency side is **derived**, so it is recomputed rather than mutated
incrementally: recomputing from the recorded request counts is idempotent and
makes the membership explainable ("it is in the pool because it was requested
57 times"), whereas an incrementally-maintained top-N is state that can drift
away from the counts it claims to summarize.

Everything here is off the request path. Recording a request is a single small
write; pre-warming is an explicit action. Neither is consulted while serving a
read, so a pool that is empty, stale or broken cannot change what a reader sees.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from fd_open_data_mcp.models import (
    PRICE_POOL_NON_MEMBER,
    PRICE_POOL_SOURCES,
    PricePoolMember,
)

#: Default number of frequently-requested symbols to keep warm.
DEFAULT_POOL_TOP_N = 50

#: Default history depth for a pre-warm, aligned with the engine's backtest
#: window cap (730 days) — a deeper warm would be paid for and never read.
DEFAULT_PREWARM_DAYS = 730


def record_request(
    session: Session, entity_type: str, entity_id: int
) -> None:
    """Note that a symbol was asked for.

    Cheap by design: one upsert per call, no membership logic. A symbol outside
    the pool still gets counted, because today's unremarkable symbol is
    tomorrow's hot one and the counts are the only evidence we have.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    member = (
        session.query(PricePoolMember)
        .filter_by(entity_type=entity_type, entity_id=entity_id)
        .one_or_none()
    )
    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    if member is None:
        session.add(PricePoolMember(
            entity_type=entity_type, entity_id=entity_id, source=PRICE_POOL_NON_MEMBER,
            request_count=1, last_requested_at=now_naive, added_at=now_naive,
        ))
    else:
        member.request_count = (member.request_count or 0) + 1
        member.last_requested_at = now_naive
    session.commit()


def add_config_member(session: Session, entity_type: str, entity_id: int) -> bool:
    """Add (or promote) a configured member. Returns True when it was new.

    Promoting means a symbol already present as ``frequency`` becomes
    ``config``: the operator's intent outranks a derived membership, and one row
    per symbol keeps the pool listing unambiguous.
    """
    member = (
        session.query(PricePoolMember)
        .filter_by(entity_type=entity_type, entity_id=entity_id)
        .one_or_none()
    )
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if member is None:
        session.add(PricePoolMember(
            entity_type=entity_type, entity_id=entity_id, source="config",
            request_count=0, added_at=now,
        ))
        session.commit()
        return True
    if member.source != "config":
        member.source = "config"
        session.commit()
        return True
    return False


def remove_config_member(session: Session, entity_type: str, entity_id: int) -> bool:
    """Remove a *configured* member.

    A frequency-derived member is left alone: deleting it would be undone by the
    next recomputation, so refusing is more honest than pretending to have
    removed something.
    """
    member = (
        session.query(PricePoolMember)
        .filter_by(entity_type=entity_type, entity_id=entity_id, source="config")
        .one_or_none()
    )
    if member is None:
        return False
    session.delete(member)
    session.commit()
    return True


def recompute_frequency_members(
    session: Session, top_n: int = DEFAULT_POOL_TOP_N
) -> dict:
    """Recompute the frequency side of the pool from recorded request counts.

    Membership is recomputed rather than maintained incrementally so it stays
    explainable from the counts, and idempotent: running it twice produces the
    same pool, which is what lets it be scheduled without bookkeeping.

    A symbol that drops out of the top N stops being a pool member. Its row is
    kept (the request counts are still evidence for a later recomputation) but
    its source is cleared to ``None``, which is why "row exists" and "is in the
    pool" are deliberately different questions.
    """
    ranked = (
        session.query(PricePoolMember)
        .order_by(PricePoolMember.request_count.desc(),
                  PricePoolMember.last_requested_at.desc().nullslast())
        .all()
    )
    top_ids = {(m.entity_type, m.entity_id) for m in ranked[:top_n]}
    added = demoted = 0
    for member in ranked:
        key = (member.entity_type, member.entity_id)
        if key in top_ids:
            if member.source == PRICE_POOL_NON_MEMBER:
                member.source = "frequency"
                added += 1
        elif member.source == "frequency":
            member.source = PRICE_POOL_NON_MEMBER
            demoted += 1
    session.commit()
    return {
        "top_n": top_n,
        "considered": len(ranked),
        "in_pool": len(top_ids),
        "added": added,
        "demoted": demoted,
    }


def pool_members(session: Session) -> list[dict]:
    """Current membership with the reason each member is there."""
    rows = (
        session.query(PricePoolMember)
        .filter(PricePoolMember.source.in_(PRICE_POOL_SOURCES))
        .order_by(PricePoolMember.source, PricePoolMember.request_count.desc())
        .all()
    )
    return [m.toDict() for m in rows]


def is_in_pool(session: Session, entity_type: str, entity_id: int) -> Optional[str]:
    """The reason a symbol is in the pool, or None when it is not."""
    member = (
        session.query(PricePoolMember)
        .filter_by(entity_type=entity_type, entity_id=entity_id)
        .one_or_none()
    )
    if member is None or member.source not in PRICE_POOL_SOURCES:
        return None
    return member.source


def prewarm(
    session: Session,
    days: int = DEFAULT_PREWARM_DAYS,
    *,
    concept_code: str = "price.close",
    limit: Optional[int] = None,
) -> dict:
    """Fill the recent window for every pool member.

    One symbol failing must not stop the others — a pre-warm that aborts on the
    first unreachable symbol would leave an operator unable to tell a network
    blip from a broken pool. Each result is recorded with its own outcome.

    Returns a per-symbol report plus counts, so "the pool is warm" is a
    checkable statement rather than a hope.
    """
    from datetime import date, timedelta

    from fd_open_data_mcp.fetch.gapfill import fill_gaps
    from fd_open_data_mcp.models import Concept

    concept = session.query(Concept).filter_by(code=concept_code).one_or_none()
    if concept is None:
        return {
            "ok": False, "reason": f"concept {concept_code!r} not found",
            "results": [], "succeeded": 0, "failed": 0, "days": days,
        }
    members = pool_members(session)
    if limit is not None:
        members = members[:limit]
    end = date.today().isoformat()
    start = (date.today() - timedelta(days=days)).isoformat()

    results = []
    succeeded = failed = 0
    for member in members:
        try:
            out = fill_gaps(
                session, concept.id, member["entity_type"],
                member["entity_id"], start, end,
            )
        except Exception as exc:  # noqa: BLE001 - one symbol must not kill the run
            failed += 1
            results.append({
                "entity_type": member["entity_type"],
                "entity_id": member["entity_id"],
                "source": member["source"],
                "ok": False, "reason": f"{type(exc).__name__}: {exc}",
            })
            continue
        ok = not out.get("fill_failed") and not out.get("gaps")
        if ok:
            succeeded += 1
        else:
            failed += 1
        results.append({
            "entity_type": member["entity_type"],
            "entity_id": member["entity_id"],
            "source": member["source"],
            "ok": ok,
            "points": len(out.get("points", [])),
            "gaps": out.get("gaps", []),
        })
    return {
        "ok": True, "days": days, "concept": concept_code,
        "results": results, "succeeded": succeeded, "failed": failed,
    }
