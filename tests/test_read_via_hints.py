"""registry-transparent-read-and-scope task 2.1: read_via hints on catalog
and search outputs (open-data-catalog spec) — one test per scenario."""
from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import text

from fd_open_data_mcp.federation import read_via_hint
from fd_open_data_mcp.models import Concept
from fd_open_data_mcp.semantic.concepts import _registry_concept_row
from fd_open_data_mcp.semantic.registry_search import _hit


REGISTRY_DDL = """
CREATE TABLE registry_entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_db VARCHAR(64),
  source_table VARCHAR(128),
  source_column VARCHAR(128),
  native_code VARCHAR(128),
  semantic_code VARCHAR(128),
  name_zh VARCHAR(255),
  name_en VARCHAR(255),
  unit VARCHAR(64),
  frequency VARCHAR(32),
  domain VARCHAR(64),
  verified INTEGER DEFAULT 0
)
"""

YEARBOOK_ENTRY = {
    "source_db": "yearbook_catalog", "native_code": "10401",
    "semantic_code": "yb.total_population", "name_zh": "年末总人口",
    "name_en": "Year-end total population", "unit": "万人",
    "frequency": "yearly", "domain": "人口",
}


def _seed_registry(session, **kw):
    session.execute(text(REGISTRY_DDL))
    row = {**YEARBOOK_ENTRY, "source_table": "yb_data", "source_column": "value",
           "verified": 1}
    row.update(kw)
    cols = ", ".join(row)
    marks = ", ".join(f":{k}" for k in row)
    session.execute(
        text(f"INSERT INTO registry_entries ({cols}) VALUES ({marks})"), row)
    session.commit()


def test_hint_points_at_the_domain_tool(session):
    """Spec open-data-catalog「Hint points at the domain tool」: a verified
    registry-only yearbook entry carries read_via naming the yearbook read
    tool with the native code as its argument."""
    _seed_registry(session)
    from fd_open_data_mcp.semantic.concepts import list_concepts_with_family

    rows = list_concepts_with_family(session, query="年末总人口")
    registry_rows = [r for r in rows if r.get("source") == "registry"]
    assert len(registry_rows) == 1
    row = registry_rows[0]
    assert row["read_via"] == {"tool": "yearbook_read", "args": {"indicator_id": 10401}}
    assert row["id"] is None and row["source_db"] == "yearbook_catalog"


def test_local_concepts_not_mislabeled(session):
    """Spec「Local concepts not mislabeled」: a local concept with local
    observations stays directly readable — no read_via label, source != registry."""
    session.add(Concept(code="gdp.total", entity_type="country", unit="亿元",
                        frequency="yearly", verified=True))
    session.commit()
    from fd_open_data_mcp.semantic.concepts import list_concepts_with_family

    rows = list_concepts_with_family(session)
    local = [r for r in rows if r["code"] == "gdp.total"]
    assert len(local) == 1
    assert "read_via" not in local[0]
    assert local[0]["source"] != "registry"


def test_search_hits_carry_read_via():
    """The search-side projection (_hit, shared by ai_search /
    semantic_search / semantic_search_unified) attaches the same hint."""
    row = SimpleNamespace(
        semantic_code="yb.total_population", name_zh="年末总人口",
        name_en="Year-end total population", source_db="yearbook_catalog",
        native_code="10401", domain="人口")
    hit = _hit(row, 0.87)
    assert hit["result_type"] == "registry"
    assert hit["read_via"] == {"tool": "yearbook_read",
                               "args": {"indicator_id": 10401}}
    assert hit["native_code"] == "10401" and hit["domain"] == "人口"


def test_unchanneled_source_carries_no_wrong_hint():
    assert read_via_hint({"source_db": "unknown_panel", "native_code": "X1"}) is None
    assert read_via_hint({"source_db": "yearbook_catalog", "native_code": None}) is None


def test_hint_covers_all_four_indicator_channels():
    assert read_via_hint({"source_db": "world_bank", "native_code": "SP.POP.TOTL"}) == {
        "tool": "wb_read", "args": {"indicator_code": "SP.POP.TOTL"}}
    assert read_via_hint({"source_db": "gta_panel", "native_code": "roa"}) == {
        "tool": "gta_read", "args": {"variable": "roa"}}
    assert read_via_hint({"source_db": "china_city_panel", "native_code": "v0913"}) == {
        "tool": "city_read", "args": {"variable": "v0913"}}
