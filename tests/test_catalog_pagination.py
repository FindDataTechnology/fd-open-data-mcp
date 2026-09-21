"""Catalog pagination (spec catalog-enumeration).

Offset paging must enumerate the full catalog exactly once, with a stable
deterministic order, a clamped page size, and an unchanged default.
"""
from __future__ import annotations

from fd_open_data_mcp.semantic.concepts import (
    LIST_DEFAULT_LIMIT,
    LIST_MAX_LIMIT,
    _clamp_limit,
    consume_indicator_defs,
    list_concept_families,
    list_concepts_with_family,
)


def _page_all(session, **kwargs) -> list[dict]:
    """Page until a short page arrives — the documented client contract."""
    out: list[dict] = []
    offset = 0
    while True:
        page = list_concepts_with_family(session, limit=2, offset=offset, **kwargs)
        if not page:
            break
        out.extend(page)
        if len(page) < 2:
            break
        offset += 2
    return out


def test_paging_enumerates_every_concept_exactly_once(session):
    consume_indicator_defs(session)
    all_rows = list_concepts_with_family(session, limit=LIST_MAX_LIMIT)
    paged = _page_all(session)

    assert len(all_rows) > 2  # the fixture seeds several concepts
    assert [r["id"] for r in paged] == [r["id"] for r in all_rows]
    ids = [r["id"] for r in paged]
    assert len(ids) == len(set(ids)), "no row may repeat across pages"


def test_consecutive_pages_do_not_overlap_or_skip(session):
    consume_indicator_defs(session)
    first = [r["id"] for r in list_concepts_with_family(session, limit=1, offset=0)]
    second = [r["id"] for r in list_concepts_with_family(session, limit=1, offset=1)]

    assert first and second
    assert not set(first) & set(second)
    # second page is the next row in the deterministic order
    ordered = [r["id"] for r in list_concepts_with_family(session, limit=LIST_MAX_LIMIT)]
    assert first == ordered[:1] and second == ordered[1:2]


def test_ordering_is_deterministic(session):
    consume_indicator_defs(session)
    a = [(r["entity_type"], r["code"], r["id"]) for r in list_concepts_with_family(session, limit=LIST_MAX_LIMIT)]
    b = [(r["entity_type"], r["code"], r["id"]) for r in list_concepts_with_family(session, limit=LIST_MAX_LIMIT)]

    assert a == b
    assert a == sorted(a), "order is (entity_type, code, id)"


def test_default_page_size_and_clamping(session):
    consume_indicator_defs(session)

    # default is unchanged (500) — visible whenever the catalog is smaller
    default_rows = list_concepts_with_family(session)
    assert len(default_rows) <= LIST_DEFAULT_LIMIT

    assert _clamp_limit(LIST_MAX_LIMIT * 10) == LIST_MAX_LIMIT
    assert _clamp_limit(0) == 1
    assert _clamp_limit(-5) == 1
    assert _clamp_limit("nonsense") == LIST_DEFAULT_LIMIT

    # a limit above the catalog size returns everything, not an error
    assert len(list_concepts_with_family(session, limit=LIST_MAX_LIMIT)) == len(default_rows)


def test_entity_type_filter_pages_independently(session):
    consume_indicator_defs(session)
    funds = _page_all(session, entity_type="fund")

    assert funds and {r["entity_type"] for r in funds} == {"fund"}
    assert [r["id"] for r in funds] == [
        r["id"] for r in list_concepts_with_family(session, entity_type="fund", limit=LIST_MAX_LIMIT)
    ]


def test_families_paginate_with_same_semantics(session):
    consume_indicator_defs(session)
    all_families = list_concept_families(session, limit=LIST_MAX_LIMIT)
    paged = []
    offset = 0
    while True:
        page = list_concept_families(session, limit=1, offset=offset)
        if not page:
            break
        paged.extend(page)
        offset += 1

    assert [f["code"] for f in paged] == [f["code"] for f in all_families]
    assert all_families, "the fixture declares families"
