"""Schema tests for the two-level concept model (add-semantic-vocabulary-core)."""
from __future__ import annotations

from sqlalchemy import create_engine, inspect, text

from fd_open_data_mcp import db as dbmod
from fd_open_data_mcp.migrate import migrate
from fd_open_data_mcp.models import Concept, ConceptFamily


def test_fresh_db_has_family_table_and_variable_column(tmp_path, monkeypatch):
    monkeypatch.setenv("FD_OPEN_DATA_MCP_DATABASE_URL", f"sqlite:///{tmp_path / 'm.db'}")
    dbmod.reset_database()
    try:
        result = migrate()
        assert "concept_families" in result["tables"]
        assert "concept_mappings" in result["tables"]
        cols = {c["name"] for c in inspect(dbmod.get_database().engine).get_columns("concepts")}
        assert "concept_code" in cols
    finally:
        dbmod.reset_database()


def test_migrate_adds_concept_code_to_existing_db_idempotently(tmp_path, monkeypatch):
    """An existing deployment predates concept_code; migrate adds it, once."""
    url = f"sqlite:///{tmp_path / 'old.db'}"
    monkeypatch.setenv("FD_OPEN_DATA_MCP_DATABASE_URL", url)
    dbmod.reset_database()

    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE fetch_log (id INTEGER PRIMARY KEY)"))
        conn.execute(text(
            "CREATE TABLE concepts (id INTEGER PRIMARY KEY, code VARCHAR(128), "
            "entity_type VARCHAR(32), measure VARCHAR(64), unit VARCHAR(64), "
            "frequency VARCHAR(32))"
        ))
    engine.dispose()

    try:
        assert "concepts.concept_code" in migrate()["added_columns"]
        assert migrate()["added_columns"] == []  # second run is a no-op
    finally:
        dbmod.reset_database()


def test_concept_linked_to_family(session):
    session.add(ConceptFamily(
        code="GDP", name_en="Gross Domestic Product", name_zh="国内生产总值",
        value_type="currency", unit_type="currency",
        dimensions=["nominal_current"], uri="https://schema.finddata.tech/concept/GDP",
    ))
    session.add(Concept(
        code="gdp", entity_type="country", measure="nominal_current", unit="usd",
        frequency="yearly", concept_code="GDP",
    ))
    session.commit()

    family = session.query(ConceptFamily).filter_by(code="GDP").one()
    assert family.toDict()["uri"] == "https://schema.finddata.tech/concept/GDP"
    assert family.toDict()["dimensions"] == ["nominal_current"]

    variable = session.query(Concept).filter_by(code="gdp").one()
    assert variable.toDict()["concept_code"] == "GDP"
    assert variable.concept_code == "GDP"


def test_existing_identity_preserved(session):
    """Same code, different measure/unit/frequency stay two distinct Variables."""
    for measure, unit in (("nominal_current", "usd"), ("real_constant", "usd")):
        session.add(Concept(code="gdp", entity_type="country", measure=measure,
                            unit=unit, frequency="yearly", concept_code="GDP"))
    session.commit()
    assert session.query(Concept).filter_by(code="gdp").count() == 2
