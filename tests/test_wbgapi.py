"""Tests for the wbgapi datasource (add-wbgapi-datasource)."""
import pytest

from fd_open_data_mcp.catalog.providers import PROVIDERS
from fd_open_data_mcp.catalog.readers import read_dict
from fd_open_data_mcp.entities.resolver import _iso2_to_iso3
from fd_open_data_mcp.semantic.mapper_llm import propose_concept


def test_wbgapi_seed_loads():
    records, errors = read_dict("fd_open_data_mcp.catalog.seeds.wbgapi", "REGISTRY", "upstream-curated")
    assert errors == []
    cmds = {r["command"] for r in records}
    assert "get_indicator_data" in cmds and "list_economies" in cmds
    gid = next(r for r in records if r["command"] == "get_indicator_data")
    assert "NY.GDP.MKTP.CD" in {c["name"] for c in gid["columns"]}


def test_wbgapi_provider_config():
    cfg = PROVIDERS["wbgapi"]
    assert cfg["reader"] == "dict"
    assert cfg["dict_module"] == "fd_open_data_mcp.catalog.seeds.wbgapi"
    assert cfg["upstream"] == "wbgapi"


def test_import_wbgapi_without_package(session):
    from fd_open_data_mcp.catalog.importer import import_provider

    r = import_provider("wbgapi", session)
    assert r["curated_count"] == 4
    assert r["errors"] == []


def test_iso2_to_iso3():
    assert _iso2_to_iso3("CN") == "CHN"
    assert _iso2_to_iso3("US") == "USA"
    assert _iso2_to_iso3("xx") is None  # graceful for unmapped


def test_seed_wbgapi_country(monkeypatch, session):
    from fd_open_data_mcp.entities import resolver, taxonomy

    monkeypatch.setattr(taxonomy, "list_entities", lambda et, db=None: (
        [{"id": 1, "iso_code": "CN"}, {"id": 2, "iso_code": "US"}, {"id": 3, "iso_code": "XX"}]
        if et == "country" else []
    ))
    r = resolver.seed_country_identifiers(session)
    assert r["wbgapi"] == 2  # CN + US mapped; XX skipped
    assert resolver.resolve_identifier(session, "country", 1, "wbgapi") == "CHN"
    assert resolver.resolve_identifier(session, "country", 3, "wbgapi") is None


def test_wbgapi_concept_mapping():
    p = propose_concept("NY.GDP.MKTP.CD")
    assert p["code"] == "gdp" and p["measure"] == "nominal_current"
    assert propose_concept("NY.GDP.MKTP.KD")["measure"] == "real_constant"
    assert propose_concept("NY.GDP.MKTP.KD.ZG")["measure"] == "growth"
    assert propose_concept("NY.GDP.PCAP.PP.CD")["measure"] == "per_capita_ppp"
    assert propose_concept("SP.POP.TOTL")["code"] == "population.total"
    assert propose_concept("EN.ATM.CO2E.KT")["code"] == "co2_emissions"
