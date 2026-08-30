"""Tests for the edgar datasource (add-edgar-datasource)."""
import sys
import types

import pytest

from fd_open_data_mcp.catalog.providers import PROVIDERS
from fd_open_data_mcp.catalog.readers import read_dict
from fd_open_data_mcp.semantic.mapper_llm import propose_concept


def test_edgar_seed_loads_via_read_dict():
    records, errors = read_dict("fd_open_data_mcp.catalog.seeds.edgar", "REGISTRY", "upstream-curated")
    assert errors == []
    commands = {r["command"] for r in records}
    assert "company_get_financials" in commands
    assert "company_get_filings" in commands
    assert len(records) == 6


def test_edgar_provider_config():
    cfg = PROVIDERS["edgar"]
    assert cfg["reader"] == "dict"
    assert cfg["dict_module"] == "fd_open_data_mcp.catalog.seeds.edgar"
    assert cfg["dict_attr"] == "REGISTRY"
    assert cfg["upstream"] == "edgar"


def test_import_edgar_without_package(session):
    """Catalog import works without edgartools installed (inline seed)."""
    from fd_open_data_mcp.catalog.importer import import_provider

    r = import_provider("edgar", session)
    assert r["curated_count"] == 6
    assert r["errors"] == []


def test_run_edgar_identity_unset(monkeypatch):
    """If EDGAR_IDENTITY is unset, the runner raises FetchError (no anonymous call)."""
    fake = types.ModuleType("edgar")
    fake.set_identity = lambda x: None
    fake.Company = type("Company", (), {})
    monkeypatch.setitem(sys.modules, "edgar", fake)
    monkeypatch.delenv("EDGAR_IDENTITY", raising=False)

    import fd_open_data_mcp.fetch.runner as runner
    monkeypatch.setattr(runner, "_EDGAR_IDENTITY_SET", False)

    with pytest.raises(runner.FetchError) as ei:
        runner.run_edgar("company_get_financials", {"ticker": "AAPL"})
    assert "EDGAR_IDENTITY" in str(ei.value)


def test_seed_edgar_us_stock(session):
    """US stocks get an edgar identifier (= ticker); non-US stocks do not.

    seed_stock_identifiers reads the ontology ``entities`` table (spec
    stock-source-identity) — seed Entity rows instead of mocking taxonomy.
    """
    from fd_open_data_mcp.entities import resolver
    from fd_open_data_mcp.models import Entity

    session.add_all([
        Entity(id=1, entity_type="stock", code="AAPL", name_en="Apple",
               metadata_json={"market": "US", "exchange": "NASDAQ"}),
        Entity(id=2, entity_type="stock", code="600519", name_zh="贵州茅台",
               metadata_json={"market": "CN", "exchange": "SSE"}),
    ])
    session.commit()
    r = resolver.seed_stock_identifiers(session)
    assert r["edgar"] == 1
    assert resolver.resolve_identifier(session, "stock", 1, "edgar") == "AAPL"
    assert resolver.resolve_identifier(session, "stock", 2, "edgar") is None


def test_edgar_financials_mapping():
    assert propose_concept("Revenue")["code"] == "financials.revenue"
    assert propose_concept("Total Assets")["code"] == "financials.total_assets"
    assert propose_concept("Net Income")["code"] == "financials.net_income"
