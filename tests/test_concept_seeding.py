"""Concept seeding from the protocol vocabulary (spec concept-variable-model)."""
from __future__ import annotations

from fd_open_data_protocol.schema import DatasourceManifest

from fd_open_data_mcp.catalog.register import register_datasource
from fd_open_data_mcp.models import Concept, ConceptFamily
from fd_open_data_mcp.semantic import concepts as concepts_mod
from fd_open_data_mcp.semantic.concepts import (
    assign_family,
    consume_indicator_defs,
    derive_family_code,
)


def test_seeds_families_and_variables(session):
    result = consume_indicator_defs(session)

    assert result["families_created"] > 0
    assert result["variables_created"] > 0
    assert session.query(ConceptFamily).filter_by(code="GDP").one().uri.endswith("/concept/GDP")

    gdp = session.query(Concept).filter_by(code="gdp", measure="nominal_current").one()
    assert gdp.concept_code == "GDP"
    assert (gdp.unit, gdp.frequency) == ("usd", "yearly")
    assert gdp.verified is True

    assert session.query(Concept).filter_by(code="population.total").one().concept_code == "Population"


def test_seeding_is_idempotent(session):
    first = consume_indicator_defs(session)
    second = consume_indicator_defs(session)

    assert (second["families_created"], second["variables_created"]) == (0, 0)
    assert session.query(ConceptFamily).count() == first["families_total"]
    assert session.query(Concept).count() == first["variables_total"]


def test_seeding_needs_no_fd_entities_indicators(session, monkeypatch):
    """The legacy sqlite source is gone: seeding works with no sibling workspace."""
    monkeypatch.setenv("FINDDATA_ROOT", "/nonexistent/workspace")
    assert not hasattr(concepts_mod, "default_entities_db")

    result = consume_indicator_defs(session)
    assert result["errors"] == []
    assert session.query(ConceptFamily).count() > 0
    assert session.query(Concept).filter_by(code="gdp").count() > 0


def test_vocabulary_qualifier_metadata_flows_through(session):
    consume_indicator_defs(session)
    gdp = session.query(ConceptFamily).filter_by(code="GDP").one()
    assert gdp.name_zh == "国内生产总值"
    assert gdp.value_type == "currency"
    assert gdp.unit_type == "currency"
    assert "nominal_current" in (gdp.dimensions or [])


def test_legacy_variable_gets_a_derived_family(session):
    session.add(Concept(code="some.legacy_metric", entity_type="country",
                        measure="", unit="", frequency="yearly"))
    session.commit()

    result = consume_indicator_defs(session)

    assert result["families_assigned"] == 1
    assert session.query(Concept).filter_by(code="some.legacy_metric").one().concept_code == "SomeLegacyMetric"
    assert session.query(ConceptFamily).filter_by(code="SomeLegacyMetric").one() is not None


def test_derivation_prefers_declared_families_by_prefix():
    known = {"priceclose": "PriceClose", "population": "Population", "gdp": "GDP"}
    assert derive_family_code("price.close", known) == "PriceClose"
    assert derive_family_code("population.total", known) == "Population"
    assert derive_family_code("gdp", known) == "GDP"
    # no prefix matches -> family-of-one keyed by the whole code
    assert derive_family_code("financials.debt_ratio", known) == "FinancialsDebtRatio"


def test_explicit_family_reference_wins(session):
    concept = Concept(code="price.close", entity_type="stock", measure="",
                      unit="currency", frequency="daily")
    session.add(concept)
    session.flush()

    assert assign_family(session, concept, explicit="NavUnit") == "NavUnit"
    assert concept.concept_code == "NavUnit"
    assert session.query(ConceptFamily).filter_by(code="NavUnit").one() is not None


def _manifest(**concept_hint) -> DatasourceManifest:
    hint = {"column": "pop", "concept": "population.total", "entity_type": "country"}
    hint.update(concept_hint)
    return DatasourceManifest(**{
        "name": "family-test", "label": "Family Test",
        "functions": [{"command": "f", "columns": [{"name": "pop"}]}],
        "concepts": [hint],
    })


def test_registration_persists_explicit_hint_family(session):
    register_datasource(_manifest(concept_family="Population"), session)
    assert session.query(Concept).filter_by(code="population.total").one().concept_code == "Population"


def test_registration_derives_family_when_hint_omits_it(session):
    """No variable is left without a family after registration."""
    register_datasource(_manifest(), session)
    assert session.query(Concept).filter_by(code="population.total").one().concept_code == "Population"
