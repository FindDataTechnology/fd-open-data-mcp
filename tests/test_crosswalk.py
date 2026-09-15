"""Concept crosswalk: mapping registry, ingest, tools, end-to-end (spec concept-crosswalk)."""
from __future__ import annotations

import pytest

from fd_open_data_mcp.models import Concept, ConceptMapping
from fd_open_data_mcp.semantic.concepts import consume_indicator_defs
from fd_open_data_mcp.semantic.crosswalk import (
    InvalidRelation,
    import_crosswalks,
    list_mappings,
    record_mapping,
    validate_relation,
)


@pytest.fixture
def population(session) -> Concept:
    """The seeded population Variable."""
    consume_indicator_defs(session)
    return session.query(Concept).filter_by(code="population.total").one()


# --- 3.1 mapping registry ----------------------------------------------------

def test_valid_relation_accepted(session, population):
    row, created = record_mapping(session, population.id, "worldbank", "SP.POP.TOTL", "exact")
    session.commit()

    assert created is True
    assert row.relation == "exact"
    assert row.provenance == "manual"


def test_invalid_relation_rejected(session, population):
    with pytest.raises(InvalidRelation) as excinfo:
        record_mapping(session, population.id, "worldbank", "SP.POP.TOTL", "equivalent_to")

    message = str(excinfo.value)
    assert "equivalent_to" in message
    for valid in ("exact", "close", "broader", "narrower", "related"):
        assert valid in message


def test_validate_relation_accepts_skos_set():
    for relation in ("exact", "close", "broader", "narrower", "related"):
        assert validate_relation(relation) == relation


def test_duplicate_assertion_is_idempotent(session, population):
    record_mapping(session, population.id, "worldbank", "SP.POP.TOTL", "exact", confidence=0.9)
    _, created = record_mapping(session, population.id, "worldbank", "SP.POP.TOTL", "exact", confidence=1.0)
    session.commit()

    assert created is False
    rows = session.query(ConceptMapping).filter_by(concept_id=population.id).all()
    assert len(rows) == 1
    assert rows[0].confidence == 1.0  # updated in place


def test_distinct_relations_are_separate_rows(session, population):
    record_mapping(session, population.id, "worldbank", "SP.POP.TOTL", "exact")
    record_mapping(session, population.id, "worldbank", "SP.POP.TOTL", "close")
    session.commit()
    assert session.query(ConceptMapping).filter_by(concept_id=population.id).count() == 2


# --- 3.2 crosswalk ingest ----------------------------------------------------

def test_ingest_shipped_crosswalks(session):
    consume_indicator_defs(session)
    result = import_crosswalks(session)

    assert result["created"] == 3
    assert result["unresolved"] == []
    rows = session.query(ConceptMapping).all()
    assert {r.provenance for r in rows} == {"registry"}
    assert {(r.vocabulary, r.term) for r in rows} == {
        ("datacommons", "Count_Person"),
        ("worldbank", "SP.POP.TOTL"),
        ("worldbank", "NY.GDP.MKTP.CD"),
    }


def test_reingest_creates_no_duplicates(session):
    consume_indicator_defs(session)
    import_crosswalks(session)
    result = import_crosswalks(session)

    assert result["created"] == 0
    assert session.query(ConceptMapping).count() == 3


def test_ingest_reports_unresolved_variables(session):
    """Assertions whose Variable is absent are reported, not silently dropped."""
    result = import_crosswalks(session)  # no consume-concepts first
    assert result["created"] == 0
    assert set(result["unresolved"]) == {"population.total/country", "gdp/country"}


# --- 3.3 MCP tools -----------------------------------------------------------

def test_record_then_list_round_trip(session, population):
    from fd_open_data_mcp.server import list_concept_mappings, record_concept_mapping

    recorded = record_concept_mapping(population.id, "wikidata", "Q33837", "exact")
    assert recorded["created"] is True

    listed = list_concept_mappings(concept_id=population.id)
    assert len(listed) == 1
    assert listed[0]["term"] == "Q33837"
    assert listed[0]["relation"] == "exact"
    assert listed[0]["reviewed"] is False
    assert listed[0]["variable"]["code"] == "population.total"


def test_reverse_lookup_by_external_term(session):
    from fd_open_data_mcp.server import list_concept_mappings

    consume_indicator_defs(session)
    import_crosswalks(session)
    hits = list_concept_mappings(vocabulary="worldbank", term="SP.POP.TOTL")

    assert len(hits) == 1
    assert hits[0]["variable"]["code"] == "population.total"
    assert hits[0]["relation"] == "exact"
    assert hits[0]["confidence"] == 1.0


def test_low_confidence_unreviewed_mapping_flagged(session, population):
    record_mapping(session, population.id, "wikidata", "Q33837", "related", confidence=0.4)
    session.commit()

    (row,) = list_mappings(session, concept_id=population.id)
    assert row["pending_review"] is True


def test_reviewed_mapping_not_flagged(session, population):
    record_mapping(session, population.id, "wikidata", "Q33837", "related",
                   confidence=0.4, reviewed=True)
    session.commit()

    (row,) = list_mappings(session, concept_id=population.id)
    assert row["pending_review"] is False


def test_high_confidence_mapping_not_flagged(session, population):
    record_mapping(session, population.id, "wikidata", "Q33837", "exact", confidence=1.0)
    session.commit()

    (row,) = list_mappings(session, concept_id=population.id)
    assert row["pending_review"] is False


def test_invalid_relation_rejected_at_tool_boundary(session, population):
    from fd_open_data_mcp.server import record_concept_mapping

    with pytest.raises(InvalidRelation):
        record_concept_mapping(population.id, "wikidata", "Q33837", "equivalent_to")


# --- 3.4 end to end ----------------------------------------------------------

def test_flagship_crosswalks_resolve_to_population(session):
    """Fresh DB: seed variables, ingest crosswalks, both flagship terms resolve."""
    consume_indicator_defs(session)
    import_crosswalks(session)

    dcid = list_mappings(session, vocabulary="datacommons", term="Count_Person")
    wb = list_mappings(session, vocabulary="worldbank", term="SP.POP.TOTL")

    assert len(dcid) == 1 and len(wb) == 1
    assert dcid[0]["concept_id"] == wb[0]["concept_id"]
    assert dcid[0]["variable"]["code"] == "population.total"
    assert dcid[0]["variable"]["family"] == "Population"

    gdp = list_mappings(session, vocabulary="worldbank", term="NY.GDP.MKTP.CD")
    assert len(gdp) == 1
    assert gdp[0]["variable"]["measure"] == "nominal_current"
    assert gdp[0]["variable"]["family"] == "GDP"
