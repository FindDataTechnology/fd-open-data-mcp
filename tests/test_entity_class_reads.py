"""Reads must resolve the entity's own class, not the row's declared one.

Measured against the production database on 2026-11-08: 343,411 observations
declared ``entity_type='stock'`` hang off entities that are **not** stocks —
333,975 on 865 fund-manager ``person`` entities, 8,500 on 11 ``industry``
entities, 936 on one ``fund``. A predicate that only compares the row's
declared type cannot see any of them, so a caller who passed such an entity id
to a stock concept read back another class's data.

The guard is deliberately narrow (``SECURITY_ENTITY_TYPES``): for hotel
country/region concepts, rows on city entities are legitimate (6,302 + 5,565
rows), so a blanket "entity type must equal concept type" rule would hide real
series. These tests pin both directions — the bad read is refused, and the
cross-class series that the product needs keeps working.
"""
from __future__ import annotations

import pytest

from fd_open_data_mcp.analysis.backend import RollupBackend
from fd_open_data_mcp.entities.resolver import (
    EntityClassMismatch,
    check_entity_identity,
)
from fd_open_data_mcp.fetch.cache import read_cache, read_cache_range
from fd_open_data_mcp.models import Concept, Entity, SemanticObservation


def _entity(session, entity_type: str, code: str) -> int:
    e = Entity(entity_type=entity_type, code=code)
    session.add(e)
    session.commit()
    return e.id


def _stock_concept(session, code: str = "price.close") -> int:
    c = Concept(code=code, entity_type="stock", frequency="daily", unit="CNY",
                verified=True)
    session.add(c)
    session.commit()
    return c.id


def _observe(session, concept_id: int, entity_id: int, date: str, value: float,
             entity_type: str = "stock") -> None:
    session.add(SemanticObservation(
        concept_id=concept_id, entity_type=entity_type, entity_id=entity_id,
        date=date, granularity="daily", value=value, unit="CNY",
        source_used="akshare",
    ))
    session.commit()


# --- the identity guard itself ------------------------------------------------

def test_stock_read_from_a_fund_manager_entity_is_refused(session):
    person = _entity(session, "person", "1298")
    with pytest.raises(EntityClassMismatch) as exc:
        check_entity_identity(session, "stock", person)
    assert "person" in str(exc.value)


def test_stock_read_from_an_industry_entity_is_refused(session):
    industry = _entity(session, "industry", "国防军工")
    with pytest.raises(EntityClassMismatch):
        check_entity_identity(session, "stock", industry)


def test_stock_read_from_a_stock_entity_passes(session):
    stock = _entity(session, "stock", "600519")
    check_entity_identity(session, "stock", stock)  # no raise


def test_cross_class_non_security_reads_are_untouched(session):
    """Hotel country concepts carry per-city rows by design — not gated."""
    city = _entity(session, "city", "上海")
    check_entity_identity(session, "country", city)  # no raise


def test_dangling_entity_id_is_not_turned_into_a_tool_error(session):
    check_entity_identity(session, "stock", 999_999)  # no raise


# --- the guard is wired into the read path -----------------------------------

def test_range_read_refuses_a_mis_attached_entity(session):
    cid = _stock_concept(session)
    person = _entity(session, "person", "1298")
    _observe(session, cid, person, "2026-06-09", 11.5)

    with pytest.raises(EntityClassMismatch):
        read_cache_range(session, cid, "stock", person, "2026-06-01", "2026-06-30")


def test_point_read_refuses_a_mis_attached_entity(session):
    cid = _stock_concept(session)
    person = _entity(session, "person", "1298")
    _observe(session, cid, person, "2026-06-09", 11.5)

    with pytest.raises(EntityClassMismatch):
        read_cache(session, cid, "stock", person, "2026-06-09")


def test_range_read_still_serves_a_real_stock(session):
    cid = _stock_concept(session)
    stock = _entity(session, "stock", "600519")
    _observe(session, cid, stock, "2026-06-09", 1500.0)

    rows = read_cache_range(session, cid, "stock", stock, "2026-06-01", "2026-06-30")
    assert [float(r.value) for r in rows] == [1500.0]


# --- the analysis backend ----------------------------------------------------

def test_backend_time_series_hides_mis_attached_rows(session):
    cid = _stock_concept(session)
    stock = _entity(session, "stock", "600519")
    person = _entity(session, "person", "1298")
    _observe(session, cid, stock, "2026-06-09", 1500.0)
    _observe(session, cid, person, "2026-06-10", 11.5)

    backend = RollupBackend()
    kept = backend.time_series(session, concept_id=cid, entity_type="stock",
                               entity_id=stock, start=None, end=None, limit=50)
    assert [float(p["value"]) for p in kept] == [1500.0]

    hidden = backend.time_series(session, concept_id=cid, entity_type="stock",
                                 entity_id=person, start=None, end=None, limit=50)
    assert hidden == []
