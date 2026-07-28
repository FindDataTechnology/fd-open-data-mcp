"""Entity-identity + ranking unit tests (task 9.2)."""
import pytest

from fd_open_data_mcp.entities.resolver import resolve_identifier
from fd_open_data_mcp.models import Concept, SourceRanking
from fd_open_data_mcp.ranking.scorer import (
    ACCESS_MIN, record_fetch_outcome, seed_ranking,
)


def test_resolve_identifier_missing_returns_none(session):
    """No registered identifier -> None -> caller skips the source (graceful degradation)."""
    assert resolve_identifier(session, "stock", 1, "akshare") is None


def test_ranking_self_tune_lower_bound(session):
    """Repeated failures cannot drop accessibility below ACCESS_MIN (design.md D7)."""
    c = Concept(code="price.close", entity_type="stock", unit="currency", frequency="daily")
    session.add(c)
    session.commit()
    seed_ranking(session, "akshare", c.id)

    for _ in range(100):
        record_fetch_outcome(session, "akshare", c.id, "429")

    row = session.query(SourceRanking).filter_by(source="akshare", concept_id=c.id).first()
    assert row.accessibility == pytest.approx(ACCESS_MIN)
    assert row.fail_count == 100
    assert row.fetch_count == 100


def test_ranking_success_raises_accessibility(session):
    c = Concept(code="price.close", entity_type="stock", unit="currency", frequency="daily")
    session.add(c)
    session.commit()
    seed_ranking(session, "akshare", c.id)
    before = session.query(SourceRanking).filter_by(
        source="akshare", concept_id=c.id).first().accessibility
    record_fetch_outcome(session, "akshare", c.id, "ok")
    after = session.query(SourceRanking).filter_by(
        source="akshare", concept_id=c.id).first().accessibility
    assert after > before
