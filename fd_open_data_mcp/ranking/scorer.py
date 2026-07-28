"""Source ranking: quality x accessibility x freshness-fit per (source x concept).

Heuristic seed on first import (design.md D7); accessibility self-tunes from
fetch_log outcomes (bounded so a single failure can't remove a source);
freshness-fit is request-dependent (spec source-ranking).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from fd_open_data_mcp.models import Concept, FetchLog, Function, SourceRanking

# Heuristic seeds: source -> (quality, accessibility).
HEURISTIC_SEEDS: dict[str, tuple[float, float]] = {
    "worldbank": (0.9, 0.8),   # authoritative, decent accessibility
    "wbgapi":    (0.9, 0.8),   # World Bank official, free no-auth API
    "edgar":     (0.85, 0.6),  # SEC official filings; rate-limited, needs identity
    "cn-gov":    (0.8, 0.75),  # official government sources
    "cn-report": (0.7, 0.7),
    "akshare":   (0.6, 0.65),  # scraped, broad, rate-limited
    "yfinance":  (0.55, 0.6),  # scraped
}
DEFAULT_SEED = (0.5, 0.5)

# Bounded self-tuning (design.md D7: a single failure cannot permanently remove a source).
ACCESS_MIN = 0.1
ACCESS_MAX = 1.0
SUCCESS_STEP = 0.02
FAIL_STEP = 0.08


def seed_ranking(session: Session, source: str, concept_id: int) -> SourceRanking:
    q, a = HEURISTIC_SEEDS.get(source, DEFAULT_SEED)
    row = session.query(SourceRanking).filter_by(source=source, concept_id=concept_id).first()
    if row is None:
        row = SourceRanking(
            source=source, concept_id=concept_id,
            quality=q, accessibility=a, freshness_fit=0.5,
        )
        session.add(row)
        session.flush()
    return row


def ensure_rankings_for_concept(session: Session, concept_id: int) -> None:
    """Seed a ranking row for every source with a binding for the concept."""
    from fd_open_data_mcp.models import ConceptBinding

    bindings = session.query(ConceptBinding).filter_by(concept_id=concept_id).all()
    sources: set[str] = set()
    for b in bindings:
        fn = session.get(Function, b.column.function_id)
        if fn is not None:
            sources.add(fn.source.name)
    for src in sources:
        seed_ranking(session, src, concept_id)
    session.commit()


def composite_score(row: SourceRanking, freshness_fit: Optional[float] = None) -> float:
    f = freshness_fit if freshness_fit is not None else row.freshness_fit
    return row.quality * row.accessibility * f


def freshness_fit_for(concept_frequency: Optional[str], requested_date: Optional[str] = None) -> float:
    """Higher score when the source frequency matches the request's recency.

    v1 coarse heuristic (spec source-ranking: a yearly source is skipped for an
    intraday request). A date-aware refinement is a future improvement.
    """
    freq = (concept_frequency or "").lower()
    if freq in ("daily", "irregular", "realtime"):
        return 0.9
    if freq in ("weekly", "monthly", "quarterly"):
        return 0.5
    if freq == "yearly":
        return 0.2
    return 0.5


def record_fetch_outcome(
    session: Session, source: str, concept_id: int, status: str,
    latency_ms: Optional[int] = None,
) -> None:
    """Append to fetch_log and adjust accessibility (bounded)."""
    session.add(FetchLog(
        source=source, concept_id=concept_id, latency_ms=latency_ms,
        status=status, timestamp=datetime.now(timezone.utc),
    ))
    row = session.query(SourceRanking).filter_by(source=source, concept_id=concept_id).first()
    if row is None:
        row = seed_ranking(session, source, concept_id)
    row.fetch_count += 1
    if status == "ok":
        row.accessibility = min(ACCESS_MAX, row.accessibility + SUCCESS_STEP)
    else:
        row.fail_count += 1
        row.accessibility = max(ACCESS_MIN, row.accessibility - FAIL_STEP)
    session.commit()


def rank_sources_for_concept(
    session: Session, concept_id: int, requested_date: Optional[str] = None,
) -> list[dict]:
    """Return candidate sources ranked best-first with their composite score."""
    ensure_rankings_for_concept(session, concept_id)
    concept = session.get(Concept, concept_id)
    freq = concept.frequency if concept else None
    ff = freshness_fit_for(freq, requested_date)
    rows = session.query(SourceRanking).filter_by(concept_id=concept_id).all()
    scored = []
    for r in rows:
        scored.append({
            "source": r.source, "score": round(composite_score(r, ff), 4),
            "quality": r.quality, "accessibility": r.accessibility,
            "freshness_fit": ff, "fetch_count": r.fetch_count, "fail_count": r.fail_count,
        })
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored
