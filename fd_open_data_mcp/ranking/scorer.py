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
    "datacommons": (0.85, 0.7), # Google-curated aggregates (UN/Census/WB provenance); free key-auth API
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


def source_frequencies_for_concept(session: Session, concept_id: int) -> dict[str, str]:
    """source name → that source's function cadence for this concept.

    A concept can be served by sources of different cadence (a yearly World Bank
    series and a daily akshare series for the same daily concept). Freshness fit
    has to be per source or it becomes the same constant multiplier for every
    candidate — which is what it used to be, so it could not reorder anything.

    First binding wins when a source has several columns for one concept; a
    function with no ``frequency`` is simply absent from the map and the caller
    falls back to the concept's cadence (unchanged behaviour for those rows).
    """
    from fd_open_data_mcp.models import ConceptBinding

    out: dict[str, str] = {}
    for b in session.query(ConceptBinding).filter_by(concept_id=concept_id).all():
        fn = session.get(Function, b.column.function_id)
        if fn is None or not fn.frequency or fn.source is None:
            continue
        out.setdefault(fn.source.name, fn.frequency)
    return out


def composite_score(row: SourceRanking, freshness_fit: Optional[float] = None) -> float:
    f = freshness_fit if freshness_fit is not None else row.freshness_fit
    return row.quality * row.accessibility * f


#: A request this recent (in days) counts as "intraday/near-real-time". A yearly
#: series cannot answer it at all, which is the one rule the spec states
#: explicitly ("a yearly source is skipped for an intraday request").
_INTRADAY_WINDOW_DAYS = 2.0

#: Fit for a cadence that cannot possibly answer a near-real-time request.
_INTRADAY_MISMATCH_FIT = 0.05


def _request_is_intraday(requested_date: Optional[str]) -> bool:
    """True when ``requested_date`` is今天/最近两天（即"要当下"的请求）。"""
    if not requested_date:
        return False
    raw = str(requested_date).strip()
    for fmt, width in (("%Y-%m-%d", 10), ("%Y-%m-%dT%H:%M:%S", 19), ("%Y-%m", 7), ("%Y", 4)):
        try:
            when = datetime.strptime(raw[:width], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        return (datetime.now(timezone.utc) - when).total_seconds() <= _INTRADAY_WINDOW_DAYS * 86400
    return False


def freshness_fit_for(frequency: Optional[str], requested_date: Optional[str] = None) -> float:
    """Higher score when a source's cadence matches how recent the request is.

    ``frequency`` is the **source's** cadence when the caller knows it (a
    function's own ``frequency``), else the concept's as a fallback — see
    :func:`rank_sources_for_concept`. Passing only the concept's frequency for
    every candidate made this a constant multiplier, and a constant cannot
    reorder anything, so the documented rule ("a yearly source is skipped for an
    intraday request") did not exist in the code.

    ``requested_date`` closes the other half: a yearly series asked for *today*
    is demoted hard (0.05), while the same series asked for two years ago keeps
    its normal 0.2 — "cannot answer this request" is a property of the pair, not
    of the cadence alone.
    """
    freq = (frequency or "").lower()
    intraday = _request_is_intraday(requested_date)
    if freq in ("daily", "irregular", "realtime"):
        return 0.9
    if freq in ("weekly", "monthly", "quarterly"):
        return 0.5
    if freq == "yearly":
        return _INTRADAY_MISMATCH_FIT if intraday else 0.2
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
    # source-proxy-health: if every registered proxy for this source is OPEN,
    # floor accessibility so the next dispatch ranks a healthy source higher.
    try:
        from fd_open_data_mcp.proxy.pool import all_proxies_unhealthy
        if all_proxies_unhealthy(session, source):
            row.accessibility = ACCESS_MIN
    except Exception:  # noqa: BLE001 - never let ranking break on proxy check
        pass
    session.commit()


def rank_sources_for_concept(
    session: Session, concept_id: int, requested_date: Optional[str] = None,
) -> list[dict]:
    """Return candidate sources ranked best-first with their composite score."""
    concept = session.get(Concept, concept_id)
    if concept is None:
        raise ValueError(f"concept {concept_id} not found")
    if concept.deprecated:
        from fd_open_data_mcp.entities.resolver import (
            ConceptDeprecated, find_canonical_replacement,
        )
        raise ConceptDeprecated(concept, find_canonical_replacement(session, concept))
    ensure_rankings_for_concept(session, concept_id)
    concept_freq = concept.frequency if concept else None
    # Per-source cadence where the binding declares one; the concept's cadence is
    # only a fallback. Using the concept's for everyone made this a constant
    # factor (audit 04 §10) — it could not change any ordering.
    src_freqs = source_frequencies_for_concept(session, concept_id)
    rows = session.query(SourceRanking).filter_by(concept_id=concept_id).all()
    scored = []
    for r in rows:
        ff = freshness_fit_for(src_freqs.get(r.source) or concept_freq, requested_date)
        scored.append({
            "source": r.source, "score": round(composite_score(r, ff), 4),
            "quality": r.quality, "accessibility": r.accessibility,
            "freshness_fit": ff, "fetch_count": r.fetch_count, "fail_count": r.fail_count,
        })
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored
