"""External entity anchors: Wikidata QID / Data Commons DCID (spec external-entity-anchors)."""
from __future__ import annotations

import pytest

from fd_open_data_protocol.schema import DatasourceManifest

from fd_open_data_mcp.catalog.register import register_datasource
from fd_open_data_mcp.entities.resolver import InvalidIdentifier, add_identifier
from fd_open_data_mcp.models import EntitySourceIdentifier
from fd_open_data_mcp.server import (
    add_entity,
    add_entity_identifier,
    get_entity,
    list_entities,
    resolve_entity,
)


@pytest.fixture
def country(session) -> dict:
    return add_entity("country", "CN", name_en="China", name_zh="中国")


# --- 4.1 anchor sources + QID validation -------------------------------------

def test_add_wikidata_anchor(country):
    row = add_entity_identifier("country", country["id"], "wikidata", "Q148")
    assert row["source"] == "wikidata"
    assert row["identifier"] == "Q148"


def test_qid_is_case_normalized(country):
    assert add_entity_identifier("country", country["id"], "wikidata", "q148")["identifier"] == "Q148"


def test_malformed_qid_rejected(session, country):
    with pytest.raises(InvalidIdentifier):
        add_identifier(session, "country", country["id"], "wikidata", "148abc")


def test_non_qid_wikidata_identifier_rejected(session, country):
    with pytest.raises(InvalidIdentifier):
        add_identifier(session, "country", country["id"], "wikidata", "China")


def test_datacommons_anchor_accepted(country):
    row = add_entity_identifier("country", country["id"], "datacommons", "country/CHN")
    assert row["identifier"] == "country/CHN"


def test_empty_dcid_rejected(session, country):
    with pytest.raises(InvalidIdentifier):
        add_identifier(session, "country", country["id"], "datacommons", "   ")


def test_non_anchor_source_unvalidated(session, country):
    """Ordinary per-source identifiers keep their pass-through behaviour."""
    assert add_identifier(session, "country", country["id"], "worldbank", "CN").identifier == "CN"


# --- 4.2 anchors declared in a manifest --------------------------------------

def _manifest(external_ids: dict) -> DatasourceManifest:
    return DatasourceManifest(**{
        "name": "anchor-test", "label": "Anchor Test",
        "functions": [],
        "entity_definitions": [{
            "entity_type": "country", "code": "CN", "name_en": "China",
            "metadata": {"external_ids": external_ids},
        }],
    })


def test_registration_persists_declared_anchors(session):
    result = register_datasource(_manifest({"wikidata": "Q148", "datacommons": "country/CHN"}), session)

    assert result["anchors"] == 2
    rows = session.query(EntitySourceIdentifier).filter_by(entity_type="country").all()
    assert {(r.source, r.identifier) for r in rows} == {
        ("wikidata", "Q148"), ("datacommons", "country/CHN"),
    }


def test_registration_skips_malformed_anchor_without_failing(session):
    """A bad anchor is logged and skipped — registration still succeeds."""
    result = register_datasource(
        _manifest({"wikidata": "not-a-qid", "datacommons": "country/CHN"}), session)

    assert result["anchors"] == 1
    assert session.query(EntitySourceIdentifier).filter_by(source="wikidata").count() == 0


def test_registration_ignores_non_dict_external_ids(session):
    result = register_datasource(_manifest(None), session)
    assert result["anchors"] == 0


# --- 4.3 resolution + entity surfaces ----------------------------------------

def test_resolve_by_qid(session, country):
    add_identifier(session, "country", country["id"], "wikidata", "Q148")
    result = resolve_entity(source="wikidata", identifier="Q148")

    assert result["match"]["entity_type"] == "country"
    assert result["match"]["code"] == "CN"


def test_resolve_by_qid_is_case_insensitive(session, country):
    add_identifier(session, "country", country["id"], "wikidata", "Q148")
    assert resolve_entity(source="wikidata", identifier="q148")["match"]["code"] == "CN"


def test_unknown_identifier_is_no_match_not_error(session):
    assert resolve_entity(source="wikidata", identifier="Q999999999")["match"] is None


def test_malformed_identifier_is_no_match(session):
    assert resolve_entity(source="wikidata", identifier="148abc")["match"] is None


def test_forward_resolution_unchanged(session, country):
    add_identifier(session, "country", country["id"], "worldbank", "CN")
    result = resolve_entity(entity_type="country", entity_id=country["id"], source="worldbank")
    assert result["identifier"] == "CN"


def test_resolve_without_arguments_reports_usage(session):
    assert resolve_entity()["identifier"] is None


def test_entity_detail_shows_anchors(session, country):
    add_identifier(session, "country", country["id"], "wikidata", "Q148")
    add_identifier(session, "country", country["id"], "datacommons", "country/CHN")

    anchors = {a["source"]: a["identifier"] for a in get_entity("country", "CN")["anchors"]}
    assert anchors == {"wikidata": "Q148", "datacommons": "country/CHN"}


def test_list_entities_shows_anchors(session, country):
    add_identifier(session, "country", country["id"], "wikidata", "Q148")

    # 工具面 3.2 起返回分页信封（rows/truncated），不再是裸列表。
    (row,) = list_entities("country")["rows"]
    assert row["anchors"] == [{"source": "wikidata", "identifier": "Q148"}]


def test_list_entities_caps_the_payload_and_says_how_to_get_more(session):
    """harden-fleet-tool-face-and-queue 3.2: one call must not dump the whole type.

    Live 2026-11-09: `list_entities(limit=100)` put 100 rows × six fields
    (metadata JSONB included) into a single tool/result; together with the
    60-entry skills list it pushed a 128k-window model into repeated
    compactions. The tool face now pages, and says how to get the rest.
    """
    from fd_open_data_mcp.server import LIST_ENTITIES_MAX_ROWS

    for i in range(LIST_ENTITIES_MAX_ROWS + 7):
        add_entity("country", f"T{i:03d}", name_en=f"Test {i}")

    page = list_entities("country")
    assert page["count"] == LIST_ENTITIES_MAX_ROWS
    assert page["total_count"] == LIST_ENTITIES_MAX_ROWS + 7
    assert page["truncated"]["returned"] == LIST_ENTITIES_MAX_ROWS
    assert "offset=" in page["truncated"]["how_to_get_more"]
    assert "metadata" not in page["rows"][0], "默认不带 metadata 字段"

    rest = list_entities("country", offset=page["count"],
                         limit=LIST_ENTITIES_MAX_ROWS)
    assert rest["count"] == 7
    assert "truncated" not in rest, "翻到最后一页就不再喊截断"

    # 要全字段就显式要 —— 上限可以被显式提高，不能被静默绕过。
    full = list_entities("country", limit=LIST_ENTITIES_MAX_ROWS + 7,
                         include_metadata=True)
    assert full["count"] == LIST_ENTITIES_MAX_ROWS + 7
    assert "metadata" in full["rows"][0]
