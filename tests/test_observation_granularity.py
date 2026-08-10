"""Observation granularity: monthly and daily observations of the same period are
distinct rows (fix-observation-time-granularity, spec observation-time-granularity).

The old date-only unique key made a monthly 2024-06-01 and a daily 2024-06-01 the
same row, so ON CONFLICT DO NOTHING silently dropped one cadence. The granularity
column in the key is the fix — this test pins that behavior.
"""
from __future__ import annotations

import pytest

from fd_open_data_mcp.models import Concept, SemanticObservation


@pytest.fixture
def concept(session):
    c = Concept(code="price.close", entity_type="stock", measure="close",
                unit="CNY", frequency="daily")
    session.add(c)
    session.commit()
    return c


def _obs(concept_id, entity_type="stock", entity_id=1, date="2024-06-01",
         granularity="day", value="1.0", source_used="test"):
    return SemanticObservation(
        concept_id=concept_id, entity_type=entity_type, entity_id=entity_id,
        date=date, granularity=granularity, value=value, source_used=source_used,
    )


def test_monthly_and_daily_coexist(session, concept):
    """Same (concept, entity, date), different granularity -> two rows, both kept."""
    session.add(_obs(concept.id, granularity="month", value="nav.jun"))
    session.add(_obs(concept.id, granularity="day", value="close.jun1"))
    session.commit()

    rows = session.query(SemanticObservation).all()
    assert len(rows) == 2
    by_g = {r.granularity: r.value for r in rows}
    assert by_g == {"month": "nav.jun", "day": "close.jun1"}


def test_upsert_do_nothing_is_idempotent_per_cadence(session, concept):
    """The writer's ON CONFLICT DO NOTHING (per (concept, entity, date, granularity))
    keeps the existing row on re-insert, mirroring the crawler's write path."""
    from sqlalchemy.dialects.sqlite import insert

    values = dict(concept_id=concept.id, entity_type="stock", entity_id=1,
                  date="2024-06-01", granularity="day", unit="", source_used="test")
    ins = insert(SemanticObservation).values(value="first", **values)
    session.execute(ins.on_conflict_do_nothing(index_elements=[
        "concept_id", "entity_type", "entity_id", "date", "granularity"]))
    session.execute(ins.on_conflict_do_nothing(index_elements=[
        "concept_id", "entity_type", "entity_id", "date", "granularity"]).values(value="second"))
    session.commit()

    rows = session.query(SemanticObservation).all()
    assert len(rows) == 1
    assert rows[0].value == "first"  # DO NOTHING keeps the existing value


def test_yearly_and_daily_distinct(session, concept):
    """Yearly 2024-12-31 and a daily row on the same calendar day are distinct."""
    session.add(_obs(concept.id, date="2024-12-31", granularity="year", value="annual"))
    session.add(_obs(concept.id, date="2024-12-31", granularity="day", value="close"))
    session.commit()

    assert session.query(SemanticObservation).count() == 2


def test_granularity_defaults_to_day(session, concept):
    """A row written without granularity (legacy write path) defaults to 'day'."""
    o = SemanticObservation(
        concept_id=concept.id, entity_type="stock", entity_id=1, date="2024-03-18",
        value="8.86", source_used="test",
    )
    session.add(o)
    session.commit()
    assert o.granularity == "day"
