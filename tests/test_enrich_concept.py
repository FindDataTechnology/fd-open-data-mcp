"""Tests for enrich-concept-identity: measure axis + column frequency/datasource."""
import pytest

from fd_open_data_mcp.models import Concept, ConceptBinding, Function, FunctionColumn, Source


def test_concept_measure_disambiguates_gdp(session):
    """GDP nominal-current vs PPP are distinct concepts (same code, different measure)."""
    session.add(Concept(code="gdp", entity_type="country", measure="nominal_current", unit="usd", frequency="yearly"))
    session.add(Concept(code="gdp", entity_type="country", measure="ppp", unit="international", frequency="yearly"))
    session.commit()
    rows = session.query(Concept).filter_by(code="gdp").all()
    assert len(rows) == 2
    measures = {r.measure for r in rows}
    assert measures == {"nominal_current", "ppp"}


def test_concept_same_5tuple_rejected(session):
    """Same (code, entity_type, measure, unit, frequency) is rejected."""
    from sqlalchemy.exc import IntegrityError

    session.add(Concept(code="gdp", entity_type="country", measure="nominal_current", unit="usd", frequency="yearly"))
    session.commit()
    session.add(Concept(code="gdp", entity_type="country", measure="nominal_current", unit="usd", frequency="yearly"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_concept_null_measure_coerced_for_uniqueness(session):
    """Two concepts with unset measure + same other fields are rejected (coerced to '')."""
    from sqlalchemy.exc import IntegrityError

    session.add(Concept(code="price.close", entity_type="stock", unit="currency", frequency="daily"))
    session.commit()
    session.add(Concept(code="price.close", entity_type="stock", unit="currency", frequency="daily"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_entity_type_vocabulary_includes_fund_and_organization():
    from fd_open_data_mcp.entities.taxonomy import ENTITY_TYPES
    assert "fund" in ENTITY_TYPES and "organization" in ENTITY_TYPES
    assert "bond" in ENTITY_TYPES and "index" in ENTITY_TYPES


def test_column_datasource_overrides_function_source(session):
    """A column with datasource=edgar is found for source 'edgar', not its function's 'akshare'."""
    src = Source(name="akshare", label="ak")
    session.add(src)
    session.flush()
    fn = Function(source_id=src.id, command="f", verified=True, scanner_mode="upstream-curated", frequency="daily")
    session.add(fn)
    session.flush()
    col = FunctionColumn(function_id=fn.id, name="revenue", datasource="edgar", frequency="yearly")
    session.add(col)
    session.flush()
    c = Concept(code="financials.revenue", entity_type="stock", measure="", unit="currency", frequency="yearly")
    session.add(c)
    session.flush()
    session.add(ConceptBinding(concept_id=c.id, column_id=col.id, confidence=0.9, provenance="manual", reviewed=True))
    session.commit()

    from fd_open_data_mcp.fetch.dispatch import _bindings_for_source
    assert len(_bindings_for_source(session, c.id, "edgar")) == 1
    assert len(_bindings_for_source(session, c.id, "akshare")) == 0


def test_column_defaults_to_function_source(session):
    """A column with no datasource defaults to its function's source."""
    src = Source(name="akshare", label="ak")
    session.add(src)
    session.flush()
    fn = Function(source_id=src.id, command="f", verified=True, scanner_mode="upstream-curated", frequency="daily")
    session.add(fn)
    session.flush()
    col = FunctionColumn(function_id=fn.id, name="close")  # no datasource / frequency
    session.add(col)
    session.flush()
    c = Concept(code="price.close", entity_type="stock", measure="", unit="currency", frequency="daily")
    session.add(c)
    session.flush()
    session.add(ConceptBinding(concept_id=c.id, column_id=col.id, confidence=0.9, provenance="manual", reviewed=True))
    session.commit()

    from fd_open_data_mcp.fetch.dispatch import _bindings_for_source
    assert len(_bindings_for_source(session, c.id, "akshare")) == 1
    assert col.frequency is None and col.datasource is None  # unset -> defaults applied at read
