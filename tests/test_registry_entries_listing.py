"""Full-registry enumeration channel (indicator-caliber-unification 1.1/1.2/1.4).

Covers ``registry_catalog.list_registry_entries`` (the library) and the
``list_registry_entries`` MCP tool (server), the PARALLEL registration-surface
channel: it serves verified AND not-verified entries, while the catalog /
search / read verified gate (``list_concepts`` 一族) must stay untouched —
the regression tests at the bottom pin that gate.

Fixture style mirrors ``test_registry_catalog.py``: isolated SQLite session,
registry table created with raw SQL only (no ORM model exists for it here).
The sample includes a ``verified IS NULL`` row — the NULL-safe half of the
``registered`` predicate is exactly what it exists for.
"""
from __future__ import annotations

import asyncio
import logging

import pytest
from sqlalchemy import text

from fd_open_data_mcp import scoping
from fd_open_data_mcp.semantic import registry_catalog
from fd_open_data_mcp.semantic.registry_catalog import list_registry_entries

_DDL = """
CREATE TABLE registry_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_db TEXT, source_table TEXT, source_column TEXT,
    native_code TEXT, semantic_code TEXT,
    name_zh TEXT, name_en TEXT,
    unit TEXT, frequency TEXT, domain TEXT,
    provenance TEXT,
    verified INTEGER, verified_at TEXT, verified_by TEXT,
    registered_at TEXT, updated_at TEXT
)
"""

# (source_db, native_code, semantic_code, name_zh, name_en, unit,
#  frequency, domain, verified) — verified may be 0 / 1 / None.
_ENTRIES = [
    ("world_bank", "NY.GDP.MKTP.CD", "wb.gdp.current_usd",
     "GDP（现价美元）", "GDP (current US$)", "US$", "A", "macro", 1),
    ("world_bank", "SP.POP.TOTL", "wb.population.total",
     "总人口", "Population, total", "persons", "A", "macro", 1),
    ("gta_panel", "nnindcd_51", "gta.industry.governance_c",
     "行业代码C-治理结构子库", "Governance structure, industry code C",
     None, None, "finance", 1),
    ("gta_panel", "zz_pending_review", "gta.industry.pending",
     "待核验变量", "Pending-review variable", None, None, "finance", 0),
    ("yearbook", "23072", "yr.23072",
     "23072", "23072", None, None, "yearbook", 0),
    # never reviewed: verified IS NULL — counts as registered, never verified
    ("yearbook", "10001", "yr.10001",
     "未核验年鉴条目", None, None, None, "yearbook", None),
    ("fd_open_data", "population.total", "population.total",
     "总人口", "Population, total", "persons", "A", "macro", 1),
]

_N_VERIFIED = sum(1 for e in _ENTRIES if e[8] == 1)
_N_REGISTERED = len(_ENTRIES) - _N_VERIFIED


def _create_registry(session):
    session.execute(text(_DDL))
    session.execute(
        text(
            "INSERT INTO registry_entries "
            "(source_db, source_table, source_column, native_code, semantic_code,"
            " name_zh, name_en, unit, frequency, domain, verified) "
            "VALUES (:source_db, :source_table, '', :native_code, :semantic_code,"
            " :name_zh, :name_en, :unit, :frequency, :domain, :verified)"
        ),
        [
            dict(
                source_db=src, source_table=f"{src}_t", native_code=native,
                semantic_code=semantic, name_zh=name_zh, name_en=name_en,
                unit=unit, frequency=freq, domain=domain, verified=verified,
            )
            for (src, native, semantic, name_zh, name_en, unit, freq,
                 domain, verified) in _ENTRIES
        ],
    )
    session.commit()


@pytest.fixture
def registry(session):
    _create_registry(session)
    return session


def _page_all(session, limit=2, **kwargs) -> list[dict]:
    """The documented client contract: advance offset until a short page."""
    out: list[dict] = []
    offset = 0
    while True:
        page = list_registry_entries(session, limit=limit, offset=offset, **kwargs)
        if not page:
            break
        out.extend(page)
        if len(page) < limit:
            break
        offset += limit
    return out


def _identity(row) -> tuple:
    return (row["source_db"], row["native_code"])


# --- (a) pagination completeness ------------------------------------------------


def test_paging_reassembles_the_full_registry_exactly_once(registry):
    full = list_registry_entries(registry, limit=1000)
    assert len(full) == len(_ENTRIES)

    paged = _page_all(registry, limit=2)
    assert paged == full
    keys = [_identity(r) for r in paged]
    assert len(keys) == len(set(keys)), "no entry may repeat across pages"


def test_short_page_marks_the_last_page(registry):
    total = len(_ENTRIES)
    tail = list_registry_entries(registry, limit=2, offset=total - 1)
    assert len(tail) == 1  # shorter than limit -> last page
    assert list_registry_entries(registry, limit=2, offset=total) == []


def test_order_is_source_db_then_native_code(registry):
    rows = list_registry_entries(registry, limit=1000)
    keys = [_identity(r) for r in rows]
    assert keys == sorted(keys)
    # spot-check the cross-source interleaving the sort implies
    assert keys[0] == ("fd_open_data", "population.total")
    assert keys[-1] == ("yearbook", "23072")


def test_limit_clamps_and_offset_floors(registry):
    full = list_registry_entries(registry, limit=1000)
    assert list_registry_entries(registry, limit=0) == full[:1]      # -> 1
    assert list_registry_entries(registry, limit=10_000) == full     # -> 1000 cap
    assert list_registry_entries(registry, limit=3, offset=-5) == \
        list_registry_entries(registry, limit=3, offset=0)           # -> 0


# --- (b) status filter: three mutually-exclusive, jointly-complete states --------


def test_status_filters_partition_the_registry(registry):
    everything = list_registry_entries(registry, limit=1000)
    verified = list_registry_entries(registry, status="verified", limit=1000)
    registered = list_registry_entries(registry, status="registered", limit=1000)

    v_keys = {_identity(r) for r in verified}
    r_keys = {_identity(r) for r in registered}
    assert not v_keys & r_keys, "verified and registered must be disjoint"
    assert v_keys | r_keys == {_identity(r) for r in everything}
    assert len(verified) == _N_VERIFIED
    assert len(registered) == _N_REGISTERED
    assert all(r["verified"] is True for r in verified)
    assert all(r["verified"] is False for r in registered)


def test_registered_covers_never_reviewed_null_rows(registry):
    """verified IS NULL (not yet reviewed) is registered, never verified."""
    verified_keys = {
        _identity(r)
        for r in list_registry_entries(registry, status="verified", limit=1000)
    }
    registered_keys = {
        _identity(r)
        for r in list_registry_entries(registry, status="registered", limit=1000)
    }
    null_row = ("yearbook", "10001")
    assert null_row in registered_keys
    assert null_row not in verified_keys


def test_status_filter_composes_with_paging(registry):
    paged = _page_all(registry, limit=2, status="registered")
    once = list_registry_entries(registry, status="registered", limit=1000)
    assert paged == once


def test_invalid_status_is_rejected(registry):
    with pytest.raises(ValueError, match="invalid status"):
        list_registry_entries(registry, status="unverified")


# --- (c) row contract ------------------------------------------------------------


def test_row_shape_is_the_enumeration_contract(registry):
    rows = list_registry_entries(registry, limit=1000)
    expected = {"source_db", "native_code", "semantic_code", "name_zh",
                "name_en", "unit", "frequency", "domain", "verified"}
    assert all(set(r) == expected for r in rows)


def test_verified_flag_is_bool_not_driver_int(registry):
    rows = list_registry_entries(registry, limit=1000)
    assert all(isinstance(r["verified"], bool) for r in rows)


# --- (d) fail-soft, same contract as read_verified_entries ------------------------


def test_missing_table_returns_empty_with_one_log(session, caplog):
    with caplog.at_level(logging.INFO, logger=registry_catalog.__name__):
        rows = list_registry_entries(session)

    assert rows == []
    missing_logs = [r for r in caplog.records if "not present" in r.getMessage()]
    assert len(missing_logs) == 1


def test_query_error_racing_the_probe_is_swallowed(session, monkeypatch, caplog):
    monkeypatch.setattr(registry_catalog, "registry_table_exists", lambda s: True)

    with caplog.at_level(logging.INFO, logger=registry_catalog.__name__):
        rows = list_registry_entries(session, status="verified")

    assert rows == []
    assert any("unreadable" in r.getMessage() for r in caplog.records)


# --- (e) the MCP tool: registration, status/scope handling ------------------------


def _registered_tool_names() -> set[str]:
    from fd_open_data_mcp.server import mcp

    return {t.name for t in asyncio.run(mcp.list_tools())}


def test_tool_is_registered():
    assert "list_registry_entries" in _registered_tool_names()


def test_tool_rejects_invalid_status_with_explicit_payload(registry):
    from fd_open_data_mcp.server import list_registry_entries as tool

    out = tool(status="unverified")
    assert out["error"] == "invalid_status"
    assert "unverified" in out["detail"]
    assert "'verified'" in out["detail"] and "'registered'" in out["detail"]


def test_tool_unscoped_returns_plain_list(registry):
    from fd_open_data_mcp.server import list_registry_entries as tool

    out = tool(limit=3)
    assert isinstance(out, list) and len(out) == 3
    assert all(isinstance(r, dict) for r in out)


def test_tool_unknown_scope_is_an_explicit_error(registry):
    from fd_open_data_mcp.server import list_registry_entries as tool

    out = tool(scope="no-such-scope")
    assert out["error"] == "unknown_scope"
    assert out["scope"] == "no-such-scope"


def test_tool_scope_filters_rows_by_source_db_and_discloses_itself(registry):
    from fd_open_data_mcp.server import list_registry_entries as tool

    scoping.scope_create(registry, "wb-only", {"source_dbs": ["world_bank"]})

    out = tool(scope="wb-only")
    assert isinstance(out, dict)
    assert {r["source_db"] for r in out["entries"]} == {"world_bank"}
    assert out["count"] == len(out["entries"])
    assert out["scope"]["name"] == "wb-only"


def test_tool_exclusionary_scope_emptiness_is_explainable(registry):
    """A scope excluding the page's rows yields a NAMED empty, not a bare []."""
    from fd_open_data_mcp.server import list_registry_entries as tool

    scoping.scope_create(registry, "wb-only", {"source_dbs": ["world_bank"]})

    # first page (limit=1) is the fd_open_data row — outside the scope
    out = tool(limit=1, offset=0, scope="wb-only")
    assert out["entries"] == [] and out["count"] == 0
    assert out["scope"]["name"] == "wb-only"
    # …while a page inside the scope still serves rows
    rows = list_registry_entries(registry, status="verified", limit=1000)
    wb_offset = next(i for i, r in enumerate(rows) if r["source_db"] == "world_bank")
    out = tool(limit=1, offset=wb_offset, status="verified", scope="wb-only")
    assert out["entries"] and out["entries"][0]["source_db"] == "world_bank"


def test_tool_explicit_unscoped_escape_returns_the_plain_list(registry):
    from fd_open_data_mcp.server import list_registry_entries as tool

    scoping.scope_create(registry, "wb-only", {"source_dbs": ["world_bank"]})

    out = tool(scope="unscoped")
    assert isinstance(out, list)
    assert {r["source_db"] for r in out} >= {"yearbook"}


# --- (f) the catalog verified gate is untouched (regression) ----------------------


def test_catalog_gate_still_excludes_unverified(registry):
    from fd_open_data_mcp.semantic.concepts import list_concepts_with_family

    rows = list_concepts_with_family(registry, limit=1000)
    registry_rows = [r for r in rows if r.get("source") == "registry"]
    assert registry_rows, "verified registry rows still join the catalog"
    assert all(r["verified"] is True for r in registry_rows)
    assert all(r["source_db"] != "yearbook" for r in registry_rows)
    assert all(r["code"] != "gta.industry.pending" for r in rows)
