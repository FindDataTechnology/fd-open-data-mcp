"""Unified indicator registry expansion of the public catalog (task 4.1).

Fixture-based tests over the repo's isolated SQLite session: the
``registry_entries`` table is created (or deliberately absent) with raw SQL
only — the production table lives on PostgreSQL, is read via raw SQL, and no
ORM model / migration exists for it in this repo.
"""
from __future__ import annotations

import logging

import pytest
from sqlalchemy import text

from fd_open_data_mcp.models import Concept
from fd_open_data_mcp.semantic import registry_catalog
from fd_open_data_mcp.semantic.concepts import (
    LIST_MAX_LIMIT,
    consume_indicator_defs,
    list_concepts_with_family,
)
from fd_open_data_mcp.semantic.registry_catalog import read_verified_entries

# SQLite stand-in for the production registry DDL (subset the reader selects;
# `verified` is an int flag under SQLite, boolean on PostgreSQL).
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

# Fixture entries mirroring the first registration batch: world_bank / gta /
# city verified, the fd_open_data mirror colliding with a native code, and a
# yearbook numeric-placeholder row that stays unverified.
_ENTRIES = [
    # (source_db, native_code, semantic_code, name_en, name_zh, unit, frequency, domain, verified)
    ("world_bank", "NY.GDP.MKTP.CD", "wb.gdp.current_usd",
     "GDP (current US$)", "GDP（现价美元）", "US$", "A", "macro", 1),
    ("gta_panel", "nnindcd_51", "gta.industry.governance_c",
     "Governance structure, industry code C", "行业代码C-治理结构子库",
     None, None, "finance", 1),
    ("china_city_panel", "v176", "city.residential_electricity",
     "Residential electricity consumption", "居民生活用电量(万千瓦时)",
     "万千瓦时", "A", "city", 1),
    ("fd_open_data", "population.total", "population.total",
     "Population, total", "总人口", "persons", "A", "macro", 1),
    ("yearbook", "23072", "yr.23072", None, "23072", None, None, "yearbook", 0),
]


def _create_registry(session):
    session.execute(text(_DDL))
    session.execute(
        text(
            "INSERT INTO registry_entries "
            "(source_db, source_table, source_column, native_code, semantic_code,"
            " name_en, name_zh, unit, frequency, domain, verified) "
            "VALUES (:source_db, :source_table, '', :native_code, :semantic_code,"
            " :name_en, :name_zh, :unit, :frequency, :domain, :verified)"
        ),
        [
            dict(
                source_db=src, source_table=f"{src}_t", native_code=native,
                semantic_code=semantic, name_en=name_en, name_zh=name_zh,
                unit=unit, frequency=freq, domain=domain, verified=verified,
            )
            for (src, native, semantic, name_en, name_zh, unit, freq,
                 domain, verified) in _ENTRIES
        ],
    )
    session.commit()


@pytest.fixture
def seeded(session):
    consume_indicator_defs(session)
    return session


@pytest.fixture
def registry(seeded, session):
    _create_registry(session)
    return session


def _page_all(session, limit=2, **kwargs) -> list[dict]:
    """Page until a short page arrives — the documented client contract."""
    out: list[dict] = []
    offset = 0
    while True:
        page = list_concepts_with_family(session, limit=limit, offset=offset, **kwargs)
        if not page:
            break
        out.extend(page)
        if len(page) < limit:
            break
        offset += limit
    return out


# --- (a) missing table: fail-soft ---------------------------------------------


def test_missing_table_returns_empty_and_keeps_native_behavior(seeded, caplog):
    with caplog.at_level(logging.INFO, logger=registry_catalog.__name__):
        entries = read_verified_entries(seeded)

    assert entries == []
    missing_logs = [r for r in caplog.records if "not present" in r.getMessage()]
    assert len(missing_logs) == 1

    rows = list_concepts_with_family(seeded, limit=LIST_MAX_LIMIT)
    native = seeded.query(Concept).order_by(
        Concept.entity_type, Concept.code, Concept.id
    ).all()
    assert [r["code"] for r in rows] == [c.code for c in native]
    assert all("native_code" not in r and "source_db" not in r for r in rows)


def test_missing_table_sql_error_is_swallowed(seeded, monkeypatch, caplog):
    """A missing-table error racing the existence check also fails soft."""
    monkeypatch.setattr(registry_catalog, "registry_table_exists", lambda s: True)

    with caplog.at_level(logging.INFO, logger=registry_catalog.__name__):
        entries = read_verified_entries(seeded)

    assert entries == []
    assert any("unreadable" in r.getMessage() for r in caplog.records)


# --- (b) union + dedupe (native wins) -----------------------------------------


def test_union_appends_registry_rows_after_native(registry):
    rows = list_concepts_with_family(registry, limit=LIST_MAX_LIMIT)

    native_codes = {"gdp", "population.total", "price.close", "nav.unit"}
    registry_rows = [r for r in rows if r.get("source") == "registry"]
    assert {r["code"] for r in registry_rows} == {
        "wb.gdp.current_usd", "gta.industry.governance_c",
        "city.residential_electricity",
    }
    # native rows unchanged in shape and still present
    assert native_codes <= {r["code"] for r in rows}
    assert all("native_code" not in r for r in rows if r.get("source") != "registry")
    # registry tail ordered by (domain, semantic_code): city < finance < macro
    assert [r["code"] for r in registry_rows] == [
        "city.residential_electricity", "gta.industry.governance_c",
        "wb.gdp.current_usd",
    ]
    # the registry rows form the contiguous tail of the merged catalog
    positions = [i for i, r in enumerate(rows) if r.get("source") == "registry"]
    assert positions == list(range(len(rows) - len(registry_rows), len(rows)))
    assert all(r["id"] is None for r in registry_rows)


def test_registry_entry_duplicating_native_code_appears_once(registry):
    """The fd_open_data mirror registers population.total = native code."""
    rows = list_concepts_with_family(registry, limit=LIST_MAX_LIMIT)

    hits = [r for r in rows if r["code"] == "population.total"]
    assert len(hits) == 1
    assert hits[0].get("source") != "registry"  # native wins
    assert "native_code" not in hits[0]


def test_deprecated_native_does_not_shadow_registry_entry(registry):
    """Legacy WDI import left deprecated concept rows sharing registry codes.

    Dedupe considers only NON-deprecated natives: a verified registry entry
    whose semantic_code equals a deprecated concept's code is still served
    (it supersedes/revives the code).
    """
    registry.add(Concept(
        code="wb.gdp.current_usd", entity_type="country", measure="",
        unit="usd", frequency="yearly", verified=True, deprecated=True,
    ))
    registry.commit()

    rows = list_concepts_with_family(registry, limit=LIST_MAX_LIMIT)
    registry_hits = [
        r for r in rows
        if r.get("source") == "registry" and r["code"] == "wb.gdp.current_usd"
    ]
    assert len(registry_hits) == 1
    assert registry_hits[0]["native_code"] == "NY.GDP.MKTP.CD"
    assert registry_hits[0]["source_db"] == "world_bank"

    # the active native collision still dedupes: population.total (mirror
    # entry) stays hidden behind the non-deprecated native row
    assert len([r for r in rows if r["code"] == "population.total"]) == 1


def test_registry_row_renders_identity_fields(registry):
    rows = list_concepts_with_family(registry, limit=LIST_MAX_LIMIT)
    gta = next(r for r in rows if r["code"] == "gta.industry.governance_c")

    assert gta["native_code"] == "nnindcd_51"
    assert gta["source_db"] == "gta_panel"
    assert gta["name_zh"] == "行业代码C-治理结构子库"
    assert gta["category"] == "finance"  # domain renders as category
    assert gta["verified"] is True
    # key parity with native rows (plus the additive identity fields)
    native = next(r for r in rows if r["code"] == "gdp")
    assert set(gta) - set(native) == {"native_code", "source_db"}


# --- (c) unverified entries never returned ------------------------------------


def test_unverified_entries_never_returned(registry):
    rows = list_concepts_with_family(registry, limit=LIST_MAX_LIMIT)

    assert all(r["code"] != "yr.23072" for r in rows)
    assert all(r.get("source_db") != "yearbook" for r in rows)
    assert all(e["source_db"] != "yearbook" for e in read_verified_entries(registry))


# --- (d) query by native code OR semantic code --------------------------------


def test_query_by_native_code_returns_both_identifiers(registry):
    rows = list_concepts_with_family(registry, query="nnindcd_51")

    assert len(rows) == 1
    assert rows[0]["code"] == "gta.industry.governance_c"
    assert rows[0]["native_code"] == "nnindcd_51"
    assert rows[0]["source_db"] == "gta_panel"


def test_query_by_semantic_code(registry):
    rows = list_concepts_with_family(registry, query="city.residential")

    assert len(rows) == 1
    assert rows[0]["native_code"] == "v176"
    assert rows[0]["source_db"] == "china_city_panel"


def test_query_by_native_code_also_hits_deduped_native_row(registry):
    """The mirror registers native_code=population.total; querying it finds the
    native row (registry duplicate was deduped away)."""
    rows = list_concepts_with_family(registry, query="POPULATION.TOTAL")

    assert {r["code"] for r in rows} == {"population.total"}
    assert all("native_code" not in r for r in rows)


def test_query_matches_native_names_too(seeded):
    # seeded Variables have no display names; give one and query by it
    gdp = seeded.query(Concept).filter_by(code="gdp").first()
    gdp.name_en = "Gross Domestic Product"
    seeded.commit()

    rows = list_concepts_with_family(seeded, query="gross domestic")

    assert "gdp" in {r["code"] for r in rows}
    assert all("gdp" in (r["code"] or "") for r in rows)


def test_query_no_match_returns_empty(seeded):
    assert list_concepts_with_family(seeded, query="zzz-no-such-indicator") == []


# --- (e) limit/offset still clamps; deterministic paging -----------------------


def test_paging_enumerates_merged_catalog_exactly_once(registry):
    full = list_concepts_with_family(registry, limit=LIST_MAX_LIMIT)
    paged = _page_all(registry, limit=2)

    assert len(full) > 2
    assert paged == full
    # identity: native rows by concepts.id, registry rows by (source_db, native_code)
    keys = [
        r["id"] if r.get("source") != "registry"
        else ("registry", r["source_db"], r["native_code"])
        for r in paged
    ]
    assert len(keys) == len(set(keys)), "no row may repeat across pages"


def test_ordering_is_deterministic(registry):
    a = list_concepts_with_family(registry, limit=LIST_MAX_LIMIT)
    b = list_concepts_with_family(registry, limit=LIST_MAX_LIMIT)
    assert a == b

    # boundary page: offset lands exactly on the native/registry seam
    native_total = len([r for r in a if r.get("source") != "registry"])
    seam = list_concepts_with_family(registry, limit=1, offset=native_total)
    assert seam and seam[0].get("source") == "registry"


def test_limit_clamp_bounds(registry):
    assert len(list_concepts_with_family(registry, limit=0)) == 1      # -> 1
    assert len(list_concepts_with_family(registry, limit=10_000)) == \
        len(list_concepts_with_family(registry, limit=LIST_MAX_LIMIT))  # -> 1000 cap


# --- filters keep their narrow semantics --------------------------------------


def test_entity_type_filter_excludes_registry_rows(registry):
    rows = list_concepts_with_family(registry, entity_type="fund")

    assert rows and {r["entity_type"] for r in rows} == {"fund"}
    assert all(r.get("source") != "registry" for r in rows)


def test_concept_family_filter_excludes_registry_rows(registry):
    rows = list_concepts_with_family(registry, concept_family="GDP")

    assert rows and {r["concept_code"] for r in rows} == {"GDP"}
    assert all(r.get("source") != "registry" for r in rows)
