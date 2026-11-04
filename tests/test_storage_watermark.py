"""Storage watermark interface (add-data-stream-engine task 2.7).

Pins: rollup freshness/age, hook drift detection (stats/coverage disagree =
hook stopped), and the cached obs_month distribution read — with NO
semantic_observations scan on the page path (the whole point of tiering).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from fd_open_data_mcp.fetch.cache import write_cache
from fd_open_data_mcp.models import (
    Concept, ConceptCoverage, ConceptSourceStats, DataCensus,
)
from fd_open_data_mcp.visibility.coverage import storage_watermark


@pytest.fixture
def concept(session):
    c = Concept(code="gdp", entity_type="country", measure="gdp",
                unit="CNY", frequency="yearly")
    session.add(c)
    session.commit()
    return c


def test_watermark_freshness_after_write(session, concept):
    write_cache(session, concept.id, "country", 1, "2024-12-31", "7", "CNY", "src_a")
    w = storage_watermark(session)
    assert w["rollup"]["concepts_covered"] == 1
    assert w["rollup"]["points_total"] == 1
    assert w["rollup"]["age_hours"] is not None
    assert w["rollup"]["age_hours"] < 1  # just written
    assert w["drift"]["hook_healthy"] is True


def test_watermark_detects_stats_without_coverage(session, concept):
    """A coverage row vanished while stats survive → hook broken (rebuild is
    the fix). Simulate by deleting the coverage row directly."""
    write_cache(session, concept.id, "country", 1, "2024-12-31", "7", "CNY", "src_a")
    session.query(ConceptCoverage).filter_by(concept_id=concept.id).delete()
    session.commit()
    w = storage_watermark(session)
    assert concept.id in w["drift"]["stats_without_coverage"]
    assert w["drift"]["hook_healthy"] is False


def test_watermark_detects_coverage_without_stats(session, concept):
    write_cache(session, concept.id, "country", 1, "2024-12-31", "7", "CNY", "src_a")
    session.query(ConceptSourceStats).filter_by(concept_id=concept.id).delete()
    session.commit()
    w = storage_watermark(session)
    assert concept.id in w["drift"]["coverage_without_stats"]
    assert w["drift"]["hook_healthy"] is False


def test_watermark_coexisting_total(session, concept):
    write_cache(session, concept.id, "country", 1, "2024-12-31", "1", "CNY", "a")
    write_cache(session, concept.id, "country", 1, "2024-12-31", "2", "CNY", "b")
    w = storage_watermark(session)
    assert w["rollup"]["coexisting_points_total"] == 1
    assert w["rollup"]["points_total"] == 1  # two sources, one point


def test_watermark_reads_cached_distribution(session, concept):
    """The distribution comes from the cached census row, never a scan."""
    session.add(DataCensus(store="gz_master", kind="local", approx_rows=99,
                           total_size_bytes=1234, exact=True,
                           obs_month_dist={"2024-12": 90, "_unmappable": 9}))
    session.commit()
    w = storage_watermark(session)
    assert w["store"]["rows"] == 99
    assert w["store"]["obs_month_dist"] == {"2024-12": 90, "_unmappable": 9}


def test_watermark_store_null_before_census(session, concept):
    write_cache(session, concept.id, "country", 1, "2024-12-31", "7", "CNY", "src_a")
    assert storage_watermark(session)["store"] is None


def test_watermark_age_scales_with_stale_computed_at(session, concept):
    write_cache(session, concept.id, "country", 1, "2024-12-31", "7", "CNY", "src_a")
    cov = session.query(ConceptCoverage).filter_by(concept_id=concept.id).one()
    cov.computed_at = datetime.now(timezone.utc) - timedelta(hours=5)
    session.commit()
    w = storage_watermark(session)
    assert 4.9 < w["rollup"]["age_hours"] < 5.1
