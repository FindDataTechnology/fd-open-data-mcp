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


def test_migrate_on_legacy_sqlite_db_bootstraps_missing_tables(tmp_path, monkeypatch):
    """Legacy SQLite DBs are not altered in place: migrate() bootstraps from
    the models (create_all) — missing tables are created, pre-existing tables
    are left untouched. In-place upgrades of existing deployments are the
    Alembic baseline chain's job (PostgreSQL only); the runtime idempotent
    ALTER mechanism was retired with it (no more ``added_columns``)."""
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
        result = migrate()
        tables = set(result["tables"])
        assert "concept_families" in tables    # missing tables bootstrapped
        assert "fetch_log" in tables           # pre-existing tables reported
        assert "added_columns" not in result   # mechanism retired into Alembic
        engine = create_engine(url)
        cols = {c["name"] for c in inspect(engine).get_columns("concepts")}
        engine.dispose()
        assert "concept_code" not in cols      # no in-place ALTER on SQLite
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
