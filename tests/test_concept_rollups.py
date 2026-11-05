"""Incremental concept rollups: write hook maintains concept_source_stats +
concept_coverage in-transaction; rebuild is the ground truth it is verified
against (add-data-stream-engine tasks 2.3/2.4).

Calibers pinned here:
  - rows = covered POINTS (coexisting sources of one point count as ONE),
    maintained exactly via the coexisting_points counter
  - hook cost is O(batch) — no full-table aggregation on the write path
  - rebuild == incremental (the verification that retires the hourly
    full-table refresh, task 2.5)
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from fd_open_data_mcp.fetch.cache import write_cache, write_cache_range
from fd_open_data_mcp.models import Concept, ConceptCoverage, ConceptSourceStats
from fd_open_data_mcp.visibility.coverage import (
    bump_rollups,
    cached_concept_coverage,
    rebuild_concept_rollups,
)


@pytest.fixture
def concept(session):
    c = Concept(code="gdp", entity_type="country", measure="gdp",
                unit="CNY", frequency="yearly")
    session.add(c)
    session.commit()
    return c


def _cov(session, cid):
    return session.query(ConceptCoverage).filter_by(concept_id=cid).one()


def test_hook_maintains_rows_and_dates(session, concept):
    write_cache_range(session, concept.id, "country", 1,
                      {"2023-12-31": 1, "2024-12-31": 2}, "CNY", "src_a")
    cov = _cov(session, concept.id)
    assert cov.rows == 2
    assert cov.latest_date == "2024-12-31"
    assert cov.sources == 1
    assert cov.last_fetch is not None

    # A second fetch of an existing point: value refresh moves last_fetch,
    # row count unchanged.
    write_cache(session, concept.id, "country", 1, "2023-12-31", "9", "CNY", "src_a")
    cov = _cov(session, concept.id)
    assert cov.rows == 2
    assert cov.latest_date == "2024-12-31"


def test_coexisting_sources_count_as_one_point(session, concept):
    """The caliber the five-column GROUP BY enforced at 8 s — now exact at
    O(1) per insert via the coexisting_points counter."""
    write_cache(session, concept.id, "country", 1, "2024-12-31", "1", "CNY", "src_a")
    write_cache(session, concept.id, "country", 1, "2024-12-31", "2", "CNY", "src_b")
    stats = session.query(ConceptSourceStats).filter_by(concept_id=concept.id).all()
    assert {s.source_used: s.rows for s in stats} == {"src_a": 1, "src_b": 1}
    cov = _cov(session, concept.id)
    assert cov.rows == 1                      # one point, two sources
    assert cov.coexisting_points == 1
    assert cov.sources == 2


def test_second_source_new_point_extends_coverage(session, concept):
    write_cache(session, concept.id, "country", 1, "2024-12-31", "1", "CNY", "src_a")
    write_cache(session, concept.id, "country", 2, "2024-12-31", "1", "CNY", "src_b")
    cov = _cov(session, concept.id)
    assert cov.rows == 2                      # two distinct points
    assert cov.coexisting_points == 0
    assert cov.latest_date == "2024-12-31"


def test_monthly_coexistence_buckets_on_obs_month(session, concept):
    """Cross-source monthly observations landing on neighboring days inside
    one month are ONE coexisting point — the 2026-11-05 audit found the
    exact-date probe counted 0 coexisting points where 8,794 month-bucket
    groups exist (sources differ by a day within the same month)."""
    write_cache(session, concept.id, "country", 1, "2026-09-30", "1", "CNY",
                "src_a", granularity="month")
    write_cache(session, concept.id, "country", 1, "2026-09-29", "2", "CNY",
                "src_b", granularity="month")
    cov = _cov(session, concept.id)
    assert cov.rows == 1                      # one month bucket, two sources
    assert cov.coexisting_points == 1

    # A different month bucket is a NEW point even when dates are adjacent
    # across the month boundary (2026-09-30 | 2026-10-01).
    write_cache(session, concept.id, "country", 1, "2026-10-01", "3", "CNY",
                "src_a", granularity="month")
    write_cache(session, concept.id, "country", 1, "2026-10-31", "4", "CNY",
                "src_b", granularity="month")
    cov = _cov(session, concept.id)
    assert cov.rows == 2
    assert cov.coexisting_points == 2

    # Rebuild (ground truth) agrees with the incremental hook.
    rebuild_concept_rollups(session, concept_id=concept.id)
    cov = _cov(session, concept.id)
    assert cov.rows == 2
    assert cov.coexisting_points == 2

    # Different granularities of the same month stay DISTINCT points
    # (uq_sem_obs point identity includes granularity).
    write_cache(session, concept.id, "country", 1, "2026-09-30", "5", "CNY",
                "src_c", granularity="quarter")
    cov = _cov(session, concept.id)
    assert cov.rows == 3
    assert cov.coexisting_points == 2


def test_daily_points_never_bucket_on_month(session, concept):
    """A day series' point IS the day: bucketing daily rows on obs_month
    collapsed a whole month of daily values into one point (~91% loss on
    the fund-aum concept, caught in the 2026-11-06 prod spot check).
    Daily coexistence still probes the exact date."""
    write_cache_range(session, concept.id, "country", 1,
                      {"2026-09-01": 1, "2026-09-02": 2, "2026-10-01": 3},
                      "CNY", "src_a", granularity="day")
    cov = _cov(session, concept.id)
    assert cov.rows == 3                      # three day points, not two
    assert cov.coexisting_points == 0

    # A second source on the same day: coexisting (exact-date probe).
    write_cache(session, concept.id, "country", 1, "2026-09-01", "9", "CNY",
                "src_b", granularity="day")
    cov = _cov(session, concept.id)
    assert cov.rows == 3
    assert cov.coexisting_points == 1

    # Rebuild (ground truth) agrees.
    rebuild_concept_rollups(session, concept_id=concept.id)
    cov = _cov(session, concept.id)
    assert cov.rows == 3
    assert cov.coexisting_points == 1


def test_rebuild_matches_incremental(session, concept):
    """rebuild == incremental on the same data — the retirement proof for
    the hourly full-table refresh."""
    write_cache_range(session, concept.id, "country", 1,
                      {"2022-12-31": 1, "2023-12-31": 2}, "CNY", "src_a")
    write_cache(session, concept.id, "country", 1, "2023-12-31", "3", "CNY", "src_b")
    write_cache(session, concept.id, "country", 2, "2024-12-31", "4", "CNY", "src_b")

    before = {s.source_used: (s.rows, s.latest_date)
              for s in session.query(ConceptSourceStats)
              .filter_by(concept_id=concept.id).all()}
    cov_before = _cov(session, concept.id)

    rebuild_concept_rollups(session, concept_id=concept.id)

    after = {s.source_used: (s.rows, s.latest_date)
             for s in session.query(ConceptSourceStats)
             .filter_by(concept_id=concept.id).all()}
    cov_after = _cov(session, concept.id)
    assert before == after
    assert (cov_before.rows, cov_before.latest_date, cov_before.sources,
            cov_before.coexisting_points) == \
           (cov_after.rows, cov_after.latest_date, cov_after.sources,
            cov_after.coexisting_points)


def test_cached_coverage_served_from_rollup(session, concept):
    """cached_concept_coverage (the /panel/data + data_stats read) sees the
    hook's updates immediately — no refresh cycle in between."""
    assert cached_concept_coverage(session)["concepts"] == []
    write_cache(session, concept.id, "country", 1, "2024-12-31", "7", "CNY", "src_a")
    out = cached_concept_coverage(session)
    assert len(out["concepts"]) == 1
    row = out["concepts"][0]
    assert row["rows"] == 1 and row["sources"] == 1
    assert row["latest_date"] == "2024-12-31"


def test_empty_batch_is_noop(session, concept):
    write_cache_range(session, concept.id, "country", 1, {}, "CNY", "src_a")
    assert session.query(ConceptCoverage).filter_by(concept_id=concept.id).one_or_none() is None
