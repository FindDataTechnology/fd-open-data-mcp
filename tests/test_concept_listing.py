"""Concept listing surfaces expose family info (spec concept-variable-model)."""
from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from fd_open_data_mcp import db as dbmod
from fd_open_data_mcp.cli import cli
from fd_open_data_mcp.semantic.concepts import (
    consume_indicator_defs,
    list_concept_families,
    list_concepts_with_family,
)


def test_list_concepts_includes_family_id_and_label(session):
    consume_indicator_defs(session)
    rows = list_concepts_with_family(session)

    assert rows
    gdp = next(r for r in rows if r["code"] == "gdp" and r["measure"] == "nominal_current")
    assert gdp["concept_code"] == "GDP"
    assert gdp["concept_family_label"] == "Gross Domestic Product"


def test_group_variables_by_family(session):
    consume_indicator_defs(session)
    gdp_vars = list_concepts_with_family(session, concept_family="GDP")

    assert len(gdp_vars) == 5  # the five seeded GDP variants
    assert {r["concept_code"] for r in gdp_vars} == {"GDP"}
    assert all(r["concept_family_label"] == "Gross Domestic Product" for r in gdp_vars)


def test_list_concepts_filters_by_entity_type(session):
    consume_indicator_defs(session)
    rows = list_concepts_with_family(session, entity_type="fund")
    assert rows and {r["entity_type"] for r in rows} == {"fund"}


def test_list_concept_families_counts_variables(session):
    consume_indicator_defs(session)
    families = {f["code"]: f for f in list_concept_families(session)}

    assert families["GDP"]["variable_count"] == 5
    assert families["Population"]["variable_count"] == 1
    assert families["Inflation"]["variable_count"] == 0  # declared, not yet seeded
    assert families["GDP"]["uri"] == "https://schema.finddata.tech/concept/GDP"


# --- CLI surface -------------------------------------------------------------

@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    monkeypatch.setenv("FD_OPEN_DATA_MCP_DATABASE_URL", f"sqlite:///{tmp_path / 'cli.db'}")
    dbmod.reset_database()
    yield
    dbmod.reset_database()


def _seeded_cli() -> CliRunner:
    runner = CliRunner()
    assert runner.invoke(cli, ["migrate"]).exit_code == 0
    assert runner.invoke(cli, ["consume-concepts"]).exit_code == 0
    return runner


def test_cli_list_concepts_shows_family(cli_env):
    result = _seeded_cli().invoke(cli, ["list-concepts", "--concept-family", "GDP"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert {row["concept_code"] for row in payload} == {"GDP"}
    assert payload[0]["concept_family_label"] == "Gross Domestic Product"


def test_cli_list_concept_families(cli_env):
    result = _seeded_cli().invoke(cli, ["list-concept-families"])

    assert result.exit_code == 0
    families = {f["code"]: f for f in json.loads(result.output)}
    assert families["GDP"]["variable_count"] == 5
