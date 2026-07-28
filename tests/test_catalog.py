"""Catalog + semantic-layer unit tests (tasks 9.1)."""
import pytest

import fd_open_data_mcp.catalog.importer as importer
from fd_open_data_mcp.models import (
    Concept, ConceptBinding, Function, FunctionColumn, Source,
)
from fd_open_data_mcp.semantic.bindings import (
    confirm_binding, dispatch_candidates, review_queue,
)


def test_concept_identity_tuple_preserves_unit(session):
    """Same code, different unit -> two distinct concepts (design.md D2)."""
    session.add(Concept(code="gdp", entity_type="country", unit="usd", frequency="yearly"))
    session.add(Concept(code="gdp", entity_type="country", unit="cny", frequency="yearly"))
    session.commit()
    rows = session.query(Concept).filter_by(code="gdp").all()
    assert len(rows) == 2


def test_concept_identity_unique(session):
    """Same (code, entity_type, unit, frequency) is rejected."""
    from sqlalchemy.exc import IntegrityError

    session.add(Concept(code="x", entity_type="stock", unit="", frequency="daily"))
    session.commit()
    session.add(Concept(code="x", entity_type="stock", unit="", frequency="daily"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_import_idempotency_and_drift(session, monkeypatch):
    """Re-import upserts (no dups) and reports added/removed sets (task 3.7)."""
    records = [{
        "command": "f1", "category": "历史行情", "description": "daily history",
        "parameters": [], "verified": True, "scanner_mode": "upstream-curated",
        "columns": [{"name": "收盘", "type": "float", "description": "-"}],
    }]
    monkeypatch.setattr(importer, "read_provider", lambda p: (records, []))

    r1 = importer.import_provider("akshare", session)
    assert r1["curated_count"] == 1
    assert r1["added"] == ["f1"]

    r2 = importer.import_provider("akshare", session)
    assert r2["curated_count"] == 1
    assert r2["added"] == []
    assert r2["removed"] == []

    # column with "-" description -> meaning=unknown (no fabrication)
    col = session.query(FunctionColumn).filter_by(name="收盘").first()
    assert col.meaning == "unknown"


def test_import_drift_reports_removed(session, monkeypatch):
    """A function absent on re-import is reported in `removed`."""
    monkeypatch.setattr(
        importer, "read_provider",
        lambda p: ([{"command": "f1", "category": "c", "description": "d",
                     "parameters": [], "verified": True,
                     "scanner_mode": "upstream-curated", "columns": []}], []),
    )
    importer.import_provider("akshare", session)
    monkeypatch.setattr(importer, "read_provider", lambda p: ([], []))
    r = importer.import_provider("akshare", session)
    assert r["removed"] == ["f1"]


def test_binding_threshold_gate(session):
    """Below-threshold bindings are withheld from dispatch and held in review (task 4.3)."""
    c = Concept(code="price.close", entity_type="stock", unit="currency", frequency="daily")
    session.add(c)
    session.flush()
    src = Source(name="akshare", label="ak")
    session.add(src)
    session.flush()
    fn = Function(source_id=src.id, command="f", verified=True, scanner_mode="upstream-curated")
    session.add(fn)
    session.flush()
    col = FunctionColumn(function_id=fn.id, name="收盘")
    session.add(col)
    session.flush()
    b = ConceptBinding(concept_id=c.id, column_id=col.id, confidence=0.3, provenance="llm")
    session.add(b)
    session.commit()

    assert dispatch_candidates(session, c.id) == []
    assert len(review_queue(session)) == 1

    confirm_binding(session, b.id)
    assert len(dispatch_candidates(session, c.id)) == 1
    assert len(review_queue(session)) == 0
