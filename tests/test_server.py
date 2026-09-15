"""MCP server tests: tool registration + a couple invocations."""
import asyncio
from pathlib import Path

import pytest

from fd_open_data_mcp.server import mcp


def test_tools_registered():
    tools = asyncio.run(mcp.list_tools())
    names = {t.name for t in tools}
    expected = {
        "import_catalog", "read", "register_datasource", "enumerate_wbgapi_indicators",
        "list_concepts", "list_concept_families", "consume_concepts", "propose_bindings",
        "rank_sources", "list_cnreport_rules", "register_discovered",
        "record_concept_mapping", "list_concept_mappings", "import_crosswalks",
    }
    assert expected <= names


def test_list_concepts_tool(session):
    from fd_open_data_mcp.server import list_concepts
    assert isinstance(list_concepts(), list)


def test_list_concept_families_tool(session):
    from fd_open_data_mcp.server import list_concept_families
    assert isinstance(list_concept_families(), list)


def _akshare_registry_present() -> bool:
    from fd_open_data_mcp.catalog.providers import PROVIDERS
    return Path(PROVIDERS["akshare"]["registry_db"]()).exists()


@pytest.mark.skipif(not _akshare_registry_present(),
                    reason="fd-akshare registry.db not in this workspace (package retired)")
def test_import_catalog_tool(session):
    from fd_open_data_mcp.server import import_catalog
    r = import_catalog("akshare")
    assert r["provider"] == "akshare" and r["curated_count"] > 0
