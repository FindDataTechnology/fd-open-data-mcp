"""MCP server tests: tool registration + a couple invocations."""
import asyncio

from fd_open_data_mcp.server import mcp


def test_tools_registered():
    tools = asyncio.run(mcp.list_tools())
    names = {t.name for t in tools}
    expected = {
        "import_catalog", "read", "register_datasource", "enumerate_wbgapi_indicators",
        "list_concepts", "consume_concepts", "propose_bindings", "rank_sources",
        "list_cnreport_rules", "register_discovered",
    }
    assert expected <= names


def test_list_concepts_tool(session):
    from fd_open_data_mcp.server import list_concepts
    assert isinstance(list_concepts(), list)


def test_import_catalog_tool(session):
    from fd_open_data_mcp.server import import_catalog
    r = import_catalog("akshare")
    assert r["provider"] == "akshare" and r["curated_count"] > 0
