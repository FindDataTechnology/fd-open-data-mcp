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
import json
import logging

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from fd_open_data_mcp.models import (
    Concept, ConceptBinding, CrawlPolicy, FetchLog, Function, FunctionColumn,
    SemanticObservation,
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
    include_deprecated: bool = True,
) -> list[dict]:
    """Per-concept coverage rows (see module docstring). Read-only.

    ``routable_only=True`` drops concepts with no dispatch-eligible binding —
    the default matches the gap-set definition in the spec (unroutable
    concepts are never counted nor selected for waves).

    ``include_deprecated=True`` keeps retired concepts in the inventory, marked
    ``deprecated`` and carrying their canonical successor. They used to be
    filtered out entirely, which meant a retirement made the concept — and every
    policy that depended on it — vanish from the gap view with no trace. Retired
    rows stay resolvable instead; the gap set excludes them separately.
    """
    q = session.query(Concept)
    if not include_deprecated:
        q = q.filter(Concept.deprecated.is_(False))
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

    # Surface attribution, one grouped scan over successful fetches. Observations
    # do not record which surface produced them, but `fetch_log` does: each ok
    # row carries the dispatched function, and the function carries the
    # capability flags. This is what makes surface coverage MEASURED instead of
    # inferred from "any row exists".
    covered_surfaces: dict[int, dict[str, bool]] = {}
    attempted: set[int] = set()
    for cid, status, fn_snap, fn_hist in session.query(
        FetchLog.concept_id, FetchLog.status, Function.bulk_snapshot, Function.bulk_history,
    ).join(Function, FetchLog.function_id == Function.id).filter(
        FetchLog.concept_id.isnot(None),
    ).all():
        attempted.add(cid)
        if status != "ok":
            continue
        flags = covered_surfaces.setdefault(
            cid, {"read": False, "snapshot": False, "history": False})
        flags["read"] = True
        if fn_snap:
            flags["snapshot"] = True
        if fn_hist:
            flags["history"] = True

    today = dt.date.today()
    # Dependency index, computed once: which policies name this concept. Retiring
    # blind is what let a policy die silently for 54 days.
    dependents_by_concept: dict[int, list[dict]] = {}
    for p in session.query(CrawlPolicy).all():
        ids = p.concept_ids or []
        if isinstance(ids, str):
            try:
                ids = json.loads(ids)
            except (TypeError, ValueError):
                ids = []
        for cid in ids if isinstance(ids, list) else []:
            dependents_by_concept.setdefault(cid, []).append(
                {"policy_id": p.id, "name": p.name, "enabled": bool(p.enabled)})
    replacement_by_concept: dict[int, object] = {}
    out: list[dict] = []
    for c in concepts:
        routable, snap, hist = _caps(c.id)
        if routable_only and not routable:
            continue
        wm_day, wm_month, wm_year = obs.get(c.id, (None, None, None))
        watermark = {"day": wm_day, "month": wm_month, "year": wm_year}[
            _granularity_for(c.frequency)]
        watermark = str(watermark)[:10] if watermark else None
        got = covered_surfaces.get(c.id, {})
        replacement = None
        if c.deprecated:
            # The successor is resolved by the same rule the read path uses
            # (`check_applicability`), so the tombstone and the error agree.
            from fd_open_data_mcp.entities.resolver import find_canonical_replacement
            replacement = find_canonical_replacement(session, c)
        out.append({
            "concept_id": c.id,
            "code": c.code,
            "name_zh": c.name_zh,
            "entity_type": c.entity_type,
            "frequency": c.frequency,
            "routable": routable,
            "bulk_snapshot": snap,
            "bulk_history": hist,
            # Retired concepts stay visible and resolvable instead of vanishing;
            # `dependents` answers "who breaks if this stays retired".
            "deprecated": bool(c.deprecated),
            "canonical_replacement": (replacement.code if replacement else None),
            "dependents": dependents_by_concept.get(c.id, []),
            # The three dispatchable surfaces a concept can expose, and whether
            # each ever produced a successful fetch. A concept is not "covered"
            # in aggregate — it is covered per surface.
            "surfaces": {"read": routable, "snapshot": snap, "history": hist},
            "surfaces_covered": {
                "read": bool(got.get("read")),
                "snapshot": bool(got.get("snapshot")),
                "history": bool(got.get("history")),
            },
            # "never attempted" is NOT "covered 0%": a surface that was never
            # tried carries no information, and reporting it as zero would turn
            # an absence of evidence into evidence of absence.
            "fetch_attempted": c.id in attempted,
            "ever_crawled": watermark is not None,
            "watermark": watermark,
            "stale": _stale(c.frequency, watermark, today),
        })
    out.sort(key=lambda r: (r["entity_type"], r["code"]))
    return out


def coverage_summary(session: Session) -> dict:
    """Aggregated gap view, reported PER SURFACE, not per concept.

    ``granularity`` and ``class_dimension`` are part of the payload: a coverage
    number without its caliber is unusable (a class-level figure must never be
    read as surface-level). Per entity_type the summary carries the three
    dispatchable surfaces with their own routable/covered counts, because a
    concept that has a snapshot binding but has only ever served realtime reads
    is NOT snapshot-covered even though "any row exists" says otherwise.
    """
    rows = coverage_inventory(session)
    surfaces = ("read", "snapshot", "history")
    per_type: dict[str, dict] = {}
    for r in rows:
        agg = per_type.setdefault(r["entity_type"], {
            "routable": 0, "covered": 0, "never_crawled": 0, "stale": 0,
            "surfaces": {s: {"routable": 0, "covered": 0, "never_attempted": 0}
                         for s in surfaces},
        })
        agg["routable"] += 1
        if r["ever_crawled"]:
            agg["covered"] += 1
            if r["stale"]:
                agg["stale"] += 1
        else:
            agg["never_crawled"] += 1
        for s in surfaces:
            if r["surfaces"][s]:
                agg["surfaces"][s]["routable"] += 1
                if r["surfaces_covered"][s]:
                    agg["surfaces"][s]["covered"] += 1
                elif not r["fetch_attempted"]:
                    # Never tried: no information, not a zero.
                    agg["surfaces"][s]["never_attempted"] += 1

    flat = {s: {k: sum(a["surfaces"][s][k] for a in per_type.values())
                for k in ("routable", "covered", "never_attempted")}
            for s in surfaces}
    legacy_covered = sum(a["covered"] for a in per_type.values())
    total_concepts = session.query(func.count(Concept.id)).scalar() or 0

    # Measurement state (knowledge-surface-integrity R1, 「未探测时申报不可得」).
    #
    # An empty catalog is NOT "0% covered": it means the probe had nothing to
    # measure — imports were never run, or the catalog table was rebuilt. Both
    # render as zeros in the counters below, and a zero reads as a verdict while
    # an empty catalog is an absence of evidence. Callers get an explicit state
    # so they can say "not measurable, catalog is empty" instead of "0%".
    if total_concepts == 0:
        state = {"state": "not_measurable",
                 "reason": "catalog holds no concepts (import-catalog has not run here)",
                 "missing": ["catalog"]}
    elif not per_type:
        # Concepts exist but none are routable: a real measurement with a real
        # (bad) result — 0% routable is the honest answer, not "unknown".
        state = {"state": "measured", "reason": None, "missing": []}
    else:
        state = {"state": "measured", "reason": None, "missing": []}
    return {
        # A number without its caliber is unusable: state both dimensions.
        "granularity": "surface",
        "class_dimension": "entity_type",
        **state,
        "total_concepts": total_concepts,
        "routable": sum(a["routable"] for a in per_type.values()),
        "covered": legacy_covered,
        "gap": sum(a["never_crawled"] for a in per_type.values()),
        "stale": sum(a["stale"] for a in per_type.values()),
        "surfaces": flat,
        # The class-level figure is kept for existing consumers but labelled:
        # it counts concepts with ANY observation, not per-surface coverage.
        "legacy": {
            "covered": legacy_covered,
            "granularity": "concept",
            "note": "class-level: counts a concept with any observation as covered; "
                    "do not read as surface-level coverage",
        },
        "per_entity_type": dict(sorted(per_type.items())),
    }


def gap_set(session: Session) -> list[dict]:
    """The wave-planner input: routable, non-retired concepts that are
    never-crawled OR stale.

    Retired concepts are excluded here (not in the inventory): they stay
    resolvable and visible, but crawling a retired indicator is never right.
    Regenerable from live data at any time (spec: concepts that gained rows
    since a previous attempt are skipped — resumability without state).
    """
    return [r for r in coverage_inventory(session)
            if not r["deprecated"] and (not r["ever_crawled"] or r["stale"])]
