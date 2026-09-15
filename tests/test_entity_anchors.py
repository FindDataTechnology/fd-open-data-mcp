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

    (row,) = list_entities("country")
    assert row["anchors"] == [{"source": "wikidata", "identifier": "Q148"}]
