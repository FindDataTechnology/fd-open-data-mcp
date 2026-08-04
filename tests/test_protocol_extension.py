"""Tests for the protocol extension (entities + relationships sections).

Phase 4 of add-entity-graph-vector-search change.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from fd_open_data_protocol.loader import load_catalog
from fd_open_data_protocol.schema import (
    DatasourceManifest,
    EntitySpec,
    RelationshipSpec,
)


EXAMPLE_STOCK = Path(__file__).resolve().parents[2] / "fd-open-data-protocol" / "examples" / "example_stock.yaml"
EXAMPLE_MACRO = Path(__file__).resolve().parents[2] / "fd-open-data-protocol" / "examples" / "example_macro.py"


def test_entity_spec_creation():
    """Test that EntitySpec can be created with various coverage modes."""
    # Universe coverage
    spec = EntitySpec(entity_type="stock", coverage="universe")
    assert spec.entity_type == "stock"
    assert spec.coverage == "universe"
    assert spec.codes is None

    # Explicit coverage
    spec = EntitySpec(entity_type="stock", coverage="explicit", codes=["AAPL", "MSFT"])
    assert spec.entity_type == "stock"
    assert spec.coverage == "explicit"
    assert spec.codes == ["AAPL", "MSFT"]


def test_relationship_spec_creation():
    """Test that RelationshipSpec can be created."""
    spec = RelationshipSpec(
        relation_type="listed_as",
        source_entity_type="company",
        target_entity_type="stock",
        resolver_module="example_stock.resolve_listed_as",
    )
    assert spec.relation_type == "listed_as"
    assert spec.source_entity_type == "company"
    assert spec.target_entity_type == "stock"
    assert spec.resolver_module == "example_stock.resolve_listed_as"


def test_manifest_with_entities_and_relationships():
    """Test that a DatasourceManifest can be created with entities and relationships."""
    manifest = DatasourceManifest(
        name="test-source",
        label="Test Source",
        functions=[],
        entities=[
            EntitySpec(entity_type="stock", coverage="explicit", codes=["AAPL"]),
        ],
        relationships=[
            RelationshipSpec(
                relation_type="listed_as",
                source_entity_type="company",
                target_entity_type="stock",
            ),
        ],
    )

    assert manifest.name == "test-source"
    assert len(manifest.entities) == 1
    assert manifest.entities[0].entity_type == "stock"
    assert len(manifest.relationships) == 1
    assert manifest.relationships[0].relation_type == "listed_as"


def test_manifest_defaults():
    """Test that entities and relationships default to empty lists."""
    manifest = DatasourceManifest(
        name="test-source",
        label="Test Source",
        functions=[],
    )

    assert manifest.entities == []
    assert manifest.relationships == []


def test_example_stock_yaml_loads_with_new_sections():
    """Test that example_stock.yaml loads with entities and relationships."""
    if not EXAMPLE_STOCK.exists():
        pytest.skip("example_stock.yaml not found")

    manifest = load_catalog(str(EXAMPLE_STOCK))

    assert manifest.name == "example-stock"
    assert len(manifest.entities) >= 1
    assert len(manifest.relationships) >= 1

    # Check entity coverage
    stock_entity = next(
        (e for e in manifest.entities if e.entity_type == "stock"), None
    )
    assert stock_entity is not None
    assert stock_entity.coverage == "explicit"
    assert "AAPL" in stock_entity.codes

    # Check relationship
    listed_as = next(
        (r for r in manifest.relationships if r.relation_type == "listed_as"), None
    )
    assert listed_as is not None
    assert listed_as.source_entity_type == "company"
    assert listed_as.target_entity_type == "stock"


def test_example_macro_py_loads_with_new_sections():
    """Test that example_macro.py loads with entities and relationships."""
    if not EXAMPLE_MACRO.exists():
        pytest.skip("example_macro.py not found")

    manifest = load_catalog(str(EXAMPLE_MACRO))

    assert manifest.name == "example-macro"
    assert len(manifest.entities) >= 1
    assert len(manifest.relationships) >= 1

    # Check entity coverage
    country_entity = next(
        (e for e in manifest.entities if e.entity_type == "country"), None
    )
    assert country_entity is not None
    assert country_entity.coverage == "universe"

    # Check relationship
    located_in = next(
        (r for r in manifest.relationships if r.relation_type == "located_in"), None
    )
    assert located_in is not None
    assert located_in.source_entity_type == "city"
    assert located_in.target_entity_type == "country"
