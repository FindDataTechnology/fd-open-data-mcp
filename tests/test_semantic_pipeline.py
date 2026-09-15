"""Full semantic-layer pipeline on a fresh DB (spec: concept-variable-model + crosswalk).

Runs the whole operator sequence end to end with no sibling workspace packages
present: migrate -> consume-concepts -> import-crosswalks -> resolve a QID ->
look a mapping up by its external term.
"""
from __future__ import annotations

from fd_open_data_mcp import db as dbmod
from fd_open_data_mcp.entities.resolver import add_identifier
from fd_open_data_mcp.migrate import migrate
from fd_open_data_mcp.models import Concept, ConceptFamily, ConceptMapping
from fd_open_data_mcp.server import add_entity, resolve_entity
from fd_open_data_mcp.semantic.concepts import consume_indicator_defs
from fd_open_data_mcp.semantic.crosswalk import import_crosswalks, list_mappings


def test_full_pipeline_without_fd_entities_indicators(tmp_path, monkeypatch):
    monkeypatch.setenv("FD_OPEN_DATA_MCP_DATABASE_URL", f"sqlite:///{tmp_path / 'pipeline.db'}")
    monkeypatch.setenv("FINDDATA_ROOT", "/nonexistent/workspace")  # no sibling packages
    dbmod.reset_database()
    try:
        # 1. schema bootstrap
        assert "concept_families" in migrate()["tables"]

        session = dbmod.get_database().get_session()
        try:
            # 2. concept families + variables from the protocol vocabulary
            seeded = consume_indicator_defs(session)
            assert seeded["families_created"] > 0
            assert seeded["variables_created"] > 0
            assert session.query(ConceptFamily).count() == seeded["families_total"]
            assert session.query(Concept).filter_by(concept_code="GDP").count() == 5

            # 3. crosswalk assertions ingested from crosswalks/*.yaml
            ingested = import_crosswalks(session)
            assert ingested["created"] == 3
            assert ingested["unresolved"] == []

            # 4. external anchor -> resolution
            country = add_entity("country", "CN", name_en="China")
            add_identifier(session, "country", country["id"], "wikidata", "Q148")
            assert resolve_entity(source="wikidata", identifier="Q148")["match"]["code"] == "CN"

            # 5. flagship mappings resolve to the population variable
            population = session.query(Concept).filter_by(code="population.total").one()
            for vocabulary, term in (("datacommons", "Count_Person"),
                                     ("worldbank", "SP.POP.TOTL")):
                (hit,) = list_mappings(session, vocabulary=vocabulary, term=term)
                assert hit["concept_id"] == population.id
                assert hit["variable"]["family"] == "Population"
                assert hit["pending_review"] is False

            assert session.query(ConceptMapping).count() == 3
        finally:
            session.close()
    finally:
        dbmod.reset_database()
