"""Coverage gap inventory (expand-crawl-coverage, spec crawl-coverage-expansion).

One read-only pass that answers, per ``(concept_id, entity_type)``:

- ``routable``   — the concept has a dispatch-eligible binding (same rule the
  planner's ``_bindings_for_source`` applies: binding provenance
  manual/sample-confirmed OR confidence >= threshold, pointing at a *verified*
  function). Unroutable concepts are excluded from the gap set entirely.
- ``ever_crawled`` — any ``semantic_observations`` row exists for the concept.
- ``watermark``  — max observed date at the concept's OWN granularity
  (day/month/year — mirrors the planner's ``_watermark`` so a monthly concept
  is never judged against a stray daily row), NULL when never crawled.
- ``stale``      — watermark older than one period of the concept's frequency.

Mutates nothing (spec: the inventory SHALL write nothing). Shared by the
``coverage`` CLI, the ``coverage_report`` MCP tool, and the digest section.
"""
from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from fd_open_data_mcp.models import (
    Concept, ConceptBinding, Function, FunctionColumn, SemanticObservation,
)
from fd_open_data_mcp.semantic.bindings import DEFAULT_THRESHOLD as DISPATCH_THRESHOLD

logger = logging.getLogger(__name__)

# One period of a concept frequency, in days (stale = watermark older than
# this). Yearly/unknown get ~a year — an unknown-frequency concept is judged
# on the calendar year because that is the coarsest bucket the planner emits.
PERIOD_DAYS = {
    "daily": 1,
    "weekly": 7,
    "monthly": 31,
    "quarterly": 92,
    "yearly": 366,
    "unknown": 366,
}


def _granularity_for(frequency: str | None) -> str:
    """Concept frequency -> observation granularity tag (mirrors the planner's)."""
    if frequency == "yearly":
        return "year"
    if frequency == "monthly":
        return "month"
    return "day"


def _stale(frequency: str | None, watermark: str | None, today: dt.date) -> bool:
    if watermark is None:
        return False  # never crawled is `gap`, not `stale`
    try:
        wm = dt.date.fromisoformat(str(watermark)[:10])
    except (TypeError, ValueError):
        return False
    period = PERIOD_DAYS.get(frequency or "unknown", 366)
    return (today - wm).days > period


def coverage_inventory(
    session: Session,
    entity_type: str | None = None,
    routable_only: bool = True,
) -> list[dict]:
    """Per-concept coverage rows (see module docstring). Read-only.

    ``routable_only=True`` drops concepts with no dispatch-eligible binding —
    the default matches the gap-set definition in the spec (unroutable
    concepts are never counted nor selected for waves).
    """
    q = session.query(Concept).filter(Concept.deprecated.is_(False))
    if entity_type:
        q = q.filter(Concept.entity_type == entity_type)
    concepts = q.all()

    # routability, batched: ONE join bindings->columns->functions per call, the
    # dispatch_candidates eligibility rule (provenance manual/sample-confirmed
    # OR confidence >= threshold) applied in Python — no per-concept queries.
    # bulk_snapshot/bulk_history capability is captured alongside (the wave
    # planner orders cheap-first on these flags).
    fn_meta = {fid: (bool(v), bool(bs), bool(bh)) for fid, v, bs, bh in
               session.query(Function.id, Function.verified,
                             Function.bulk_snapshot, Function.bulk_history).all()}
    fn_ids_by_concept: dict[int, list[tuple[float, str, int]]] = {}
    for cid, conf, prov, fn_id in (
        session.query(ConceptBinding.concept_id, ConceptBinding.confidence,
                      ConceptBinding.provenance, FunctionColumn.function_id)
        .join(FunctionColumn, ConceptBinding.column_id == FunctionColumn.id)
        .all()
    ):
        fn_ids_by_concept.setdefault(cid, []).append((conf, prov, fn_id))

    def _caps(cid: int) -> tuple[bool, bool, bool]:
        """(routable, has bulk_snapshot binding, has bulk_history binding)."""
        routable = snap = hist = False
        for conf, prov, fn_id in fn_ids_by_concept.get(cid, ()):
            eligible = prov in ("manual", "sample-confirmed") or conf >= DISPATCH_THRESHOLD
            meta = fn_meta.get(fn_id)
            if not (eligible and meta and meta[0]):
                continue
            routable = True
            snap = snap or meta[1]
            hist = hist or meta[2]
        return routable, snap, hist

    # watermarks at each granularity, one grouped scan over observations
    obs = {row[0]: (row[1], row[2], row[3]) for row in session.query(
        SemanticObservation.concept_id,
        func.max(case((SemanticObservation.granularity == "day",
                       SemanticObservation.date))),
        func.max(case((SemanticObservation.granularity == "month",
                       SemanticObservation.date))),
        func.max(case((SemanticObservation.granularity == "year",
                       SemanticObservation.date))),
    ).group_by(SemanticObservation.concept_id).all()}

    today = dt.date.today()
    out: list[dict] = []
    for c in concepts:
        routable, snap, hist = _caps(c.id)
        if routable_only and not routable:
            continue
        wm_day, wm_month, wm_year = obs.get(c.id, (None, None, None))
        watermark = {"day": wm_day, "month": wm_month, "year": wm_year}[
            _granularity_for(c.frequency)]
        watermark = str(watermark)[:10] if watermark else None
        out.append({
            "concept_id": c.id,
            "code": c.code,
            "name_zh": c.name_zh,
            "entity_type": c.entity_type,
            "frequency": c.frequency,
            "routable": routable,
            "bulk_snapshot": snap,
            "bulk_history": hist,
            "ever_crawled": watermark is not None,
            "watermark": watermark,
            "stale": _stale(c.frequency, watermark, today),
        })
    out.sort(key=lambda r: (r["entity_type"], r["code"]))
    return out


def coverage_summary(session: Session) -> dict:
    """Aggregated gap view: per entity_type, routable/covered/never/stale.

    This is the number the digest and ``coverage_report`` lead with —
    "covered vs routable" is the single metric expansion moves.
    """
    rows = coverage_inventory(session)
    per_type: dict[str, dict] = {}
    for r in rows:
        agg = per_type.setdefault(r["entity_type"], {
            "routable": 0, "covered": 0, "never_crawled": 0, "stale": 0,
        })
        agg["routable"] += 1
        if r["ever_crawled"]:
            agg["covered"] += 1
            if r["stale"]:
                agg["stale"] += 1
        else:
            agg["never_crawled"] += 1
    return {
        "total_concepts": session.query(func.count(Concept.id)).scalar(),
        "routable": sum(a["routable"] for a in per_type.values()),
        "covered": sum(a["covered"] for a in per_type.values()),
        "gap": sum(a["never_crawled"] for a in per_type.values()),
        "stale": sum(a["stale"] for a in per_type.values()),
        "per_entity_type": dict(sorted(per_type.items())),
    }


def gap_set(session: Session) -> list[dict]:
    """The wave-planner input: routable concepts that are never-crawled OR stale.

    Regenerable from live data at any time (spec: concepts that gained rows
    since a previous attempt are skipped — resumability without state).
    """
    return [r for r in coverage_inventory(session) if not r["ever_crawled"] or r["stale"]]
