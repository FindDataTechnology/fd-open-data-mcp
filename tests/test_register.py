"""Tests for register_datasource (fd-open-data-mcp ingest of a manifest)."""
from pathlib import Path

from fd_open_data_protocol.loader import load_catalog
from fd_open_data_protocol.schema import (
    ColumnSpec, ConceptHint, DatasourceManifest, FunctionSpec,
)

from fd_open_data_mcp.catalog.register import register_datasource
from fd_open_data_mcp.models import (
    Concept, ConceptBinding, Function, FunctionColumn, Source,
)

EXAMPLE = Path(__file__).resolve().parents[2] / "fd-open-data-protocol" / "examples" / "example_stock.yaml"


def _manifest() -> DatasourceManifest:
    return DatasourceManifest(
        name="test-src", label="Test Src",
        functions=[FunctionSpec(
            command="get_hist", frequency="daily",
            columns=[ColumnSpec(name="close", type="float", frequency="daily")],
        )],
        concepts=[ConceptHint(
            column="close", concept="price.close", entity_type="stock",
            unit="currency", frequency="daily",
        )],
    )


def test_register_upserts(session):
    r = register_datasource(_manifest(), session)
    assert r["name"] == "test-src"
    assert (r["functions"], r["columns"], r["concepts"], r["bindings"]) == (1, 1, 1, 1)
    src = session.query(Source).filter_by(name="test-src").first()
    fn = session.query(Function).filter_by(source_id=src.id).first()
    assert fn.command == "get_hist" and fn.frequency == "daily"
    col = session.query(FunctionColumn).filter_by(function_id=fn.id, name="close").first()
    assert col.frequency == "daily"
    c = session.query(Concept).filter_by(code="price.close", entity_type="stock").first()
    assert c is not None
    assert session.query(ConceptBinding).filter_by(concept_id=c.id, column_id=col.id).first() is not None


def test_register_idempotent(session):
    register_datasource(_manifest(), session)
    r2 = register_datasource(_manifest(), session)
    assert (r2["functions"], r2["columns"], r2["concepts"], r2["bindings"]) == (0, 0, 0, 0)
    assert session.query(Function).count() == 1
    assert session.query(ConceptBinding).count() == 1


def test_register_example_stock_manifest(session):
    if not EXAMPLE.exists():
        import pytest
        pytest.skip("example_stock.yaml not found")
    r = register_datasource(load_catalog(str(EXAMPLE)), session)
    assert r["name"] == "example-stock" and r["functions"] >= 1
    src = session.query(Source).filter_by(name="example-stock").first()
    assert src is not None
