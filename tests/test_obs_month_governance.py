"""obs_month derived partition bucket: mapping + write-site maintenance
(add-data-stream-engine task 2.2; census 2026-11-04 patterns are pinned here
so the SQL CASE in alembic 0013 and this helper cannot drift apart silently).
"""
from __future__ import annotations

import pytest

from fd_open_data_mcp.fetch.cache import write_cache, write_cache_range
from fd_open_data_mcp.models import Concept, SemanticObservation
from fd_open_data_mcp.semantic.dates import obs_month_of


@pytest.mark.parametrize("raw,expected", [
    ("2024-06-01", "2024-06"),          # canonical
    ("1986-02-01", "1986-02"),          # historical canonical
    ("2026-10-06T02", "2026-10"),       # hour-truncated (hour grain rows)
    ("2026-10", "2026-10"),             # bare month
    ("2025", "2025-12"),                # bare year (cnreport defect shape)
    ("1960", "1960-12"),                # bare year, oldest in prod
    ("2026-W35", "2026-08"),            # ISO week -> Monday's month
    ("LATEST", None),                   # sentinel -> default partition
    ("", None),
    (None, None),
])
def test_obs_month_mapping(raw, expected):
    assert obs_month_of(raw) == expected


@pytest.fixture
def concept(session):
    c = Concept(code="price.close", entity_type="stock", measure="close",
                unit="CNY", frequency="daily")
    session.add(c)
    session.commit()
    return c


def test_write_cache_sets_bucket(session, concept):
    write_cache(session, concept.id, "stock", 1, "2024-06-01", "1.0", "CNY", "test")
    row = session.query(SemanticObservation).one()
    assert row.obs_month == "2024-06"


def test_write_cache_repair_sets_bucket_on_update(session, concept):
    """A legacy row written before the bucket existed gets its bucket filled
    on the next fetch of the same point (value untouched, bucket backfilled)."""
    session.add(SemanticObservation(
        concept_id=concept.id, entity_type="stock", entity_id=1,
        date="2025", granularity="day", value="old", source_used="test",
        obs_month=None))
    session.commit()

    write_cache(session, concept.id, "stock", 1, "2025", "new", "CNY", "test")
    row = session.query(SemanticObservation).one()
    assert row.value == "new"
    assert row.obs_month == "2025-12"


def test_write_cache_range_sets_bucket_per_row(session, concept):
    n = write_cache_range(session, concept.id, "stock", 1, {
        "2024-06-01": 1.0,
        "2025": 2.0,        # bare year lands in the December bucket
        "2026-W35": 3.0,    # ISO week lands in its Monday month
        "LATEST": 4.0,      # sentinel -> NULL bucket (future default partition)
    }, "CNY", "test")
    assert n == 4
    buckets = {r.date: r.obs_month
               for r in session.query(SemanticObservation).all()}
    assert buckets == {
        "2024-06-01": "2024-06",
        "2025": "2025-12",
        "2026-W35": "2026-08",
        "LATEST": None,
    }
