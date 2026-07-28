"""Tests for ranking (composite score, freshness, seed, ensure) + cache (TTL, conflict)."""
from datetime import datetime, timedelta, timezone

import pytest

from fd_open_data_mcp.fetch.cache import is_stale, write_cache
from fd_open_data_mcp.models import (
    Concept, ConceptBinding, Function, FunctionColumn, SemanticObservation, Source,
    SourceRanking,
)
from fd_open_data_mcp.ranking.scorer import (
    HEURISTIC_SEEDS, composite_score, ensure_rankings_for_concept, freshness_fit_for,
)


def test_composite_score():
    r = SourceRanking(source="akshare", concept_id=1, quality=0.6, accessibility=0.5, freshness_fit=0.9)
    assert composite_score(r) == pytest.approx(0.6 * 0.5 * 0.9)


def test_freshness_fit():
    assert freshness_fit_for("daily") == 0.9
    assert freshness_fit_for("realtime") == 0.9
    assert freshness_fit_for("yearly") == 0.2
    assert freshness_fit_for("monthly") == 0.5


def test_heuristic_seeds():
    assert HEURISTIC_SEEDS["akshare"] == (0.6, 0.65)
    assert HEURISTIC_SEEDS["edgar"] == (0.85, 0.6)
    assert HEURISTIC_SEEDS["wbgapi"] == (0.9, 0.8)


def test_ensure_rankings(session):
    c = Concept(code="price.close", entity_type="stock", measure="", unit="currency", frequency="daily")
    session.add(c); session.flush()
    src = Source(name="akshare", label="ak")
    session.add(src); session.flush()
    fn = Function(source_id=src.id, command="f", verified=True, scanner_mode="upstream-curated")
    session.add(fn); session.flush()
    col = FunctionColumn(function_id=fn.id, name="close")
    session.add(col); session.flush()
    session.add(ConceptBinding(concept_id=c.id, column_id=col.id, confidence=0.9, provenance="manual", reviewed=True))
    session.commit()
    ensure_rankings_for_concept(session, c.id)
    assert session.query(SourceRanking).filter_by(source="akshare", concept_id=c.id).first() is not None


def test_is_stale_ttl():
    stale = SemanticObservation(
        concept_id=1, entity_type="stock", entity_id=1, date="2024-01-01",
        value="1", unit="currency", source_used="akshare",
        fetched_at=datetime.now(timezone.utc) - timedelta(hours=25),
    )
    assert is_stale(stale, "daily") is True
    fresh = SemanticObservation(
        concept_id=1, entity_type="stock", entity_id=1, date="2024-01-01",
        value="1", unit="currency", source_used="akshare",
        fetched_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    assert is_stale(fresh, "daily") is False


def test_write_cache_conflict_policy(session):
    c = Concept(code="price.close", entity_type="stock", measure="", unit="currency", frequency="daily")
    session.add(c); session.flush()
    write_cache(session, c.id, "stock", 1, "2024-01-01", "100", "currency", "akshare")
    write_cache(session, c.id, "stock", 1, "2024-01-01", "200", "currency", "yfinance")
    rows = session.query(SemanticObservation).filter_by(
        concept_id=c.id, entity_type="stock", entity_id=1, date="2024-01-01",
    ).all()
    assert len(rows) == 1  # one row, no merge
    assert rows[0].source_used == "yfinance"  # last writer
    assert rows[0].value == "200"
