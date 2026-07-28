"""Tests for wbgapi indicator enumeration (add-wbgapi-indicator-enumeration)."""
import sys
import types

import pytest

from fd_open_data_mcp.catalog.importer import import_provider
from fd_open_data_mcp.models import (
    Concept, ConceptBinding, Function, FunctionColumn, Source,
)


class _FakeFeatureset:
    def __init__(self, items):
        self.items = items


class _FakeSeries:
    def __init__(self, items, error=None):
        self._items = items
        self._error = error

    def info(self, db=None):
        if self._error:
            raise self._error
        return _FakeFeatureset(self._items)


def _install_fake_wbgapi(monkeypatch, items=None, error=None):
    fake = types.ModuleType("wbgapi")
    fake.series = _FakeSeries(items or [], error=error)
    monkeypatch.setitem(sys.modules, "wbgapi", fake)


def test_enumerate_creates_columns_concepts_bindings(session, monkeypatch):
    import_provider("wbgapi", session)
    _install_fake_wbgapi(monkeypatch, [
        {"id": "NY.GDP.MKTP.CD", "value": "GDP (current US$)"},
        {"id": "SP.POP.TOTL", "value": "Population, total"},
    ])
    from fd_open_data_mcp.catalog.wbgapi_enumerate import enumerate_wbgapi_indicators

    r = enumerate_wbgapi_indicators(session)
    assert r["imported"] == 2 and r["total"] == 2 and r["errors"] == []

    src = session.query(Source).filter_by(name="wbgapi").first()
    fn = session.query(Function).filter_by(source_id=src.id, command="get_indicator_data").first()
    col = session.query(FunctionColumn).filter_by(function_id=fn.id, name="NY.GDP.MKTP.CD").first()
    assert col is not None and col.description == "GDP (current US$)"
    c = session.query(Concept).filter_by(code="NY.GDP.MKTP.CD", entity_type="country").first()
    assert c is not None and c.frequency == "yearly" and c.unit == "unknown"
    b = session.query(ConceptBinding).filter_by(concept_id=c.id, column_id=col.id).first()
    assert b is not None and b.provenance == "manual" and b.reviewed is True


def test_enumerate_is_idempotent(session, monkeypatch):
    import_provider("wbgapi", session)
    items = [{"id": "NY.GDP.MKTP.CD", "value": "GDP"}, {"id": "SP.POP.TOTL", "value": "Pop"}]
    _install_fake_wbgapi(monkeypatch, items)
    from fd_open_data_mcp.catalog.wbgapi_enumerate import enumerate_wbgapi_indicators

    r1 = enumerate_wbgapi_indicators(session)
    r2 = enumerate_wbgapi_indicators(session)
    assert r1["imported"] == 2
    assert r2["imported"] == 0  # no new bindings on re-run
    assert r2["total"] == 2
    assert session.query(ConceptBinding).count() == 2  # no duplicates


def test_enumerate_network_failure_no_partial_write(session, monkeypatch):
    import_provider("wbgapi", session)
    _install_fake_wbgapi(monkeypatch, error=RuntimeError("network unreachable"))
    from fd_open_data_mcp.catalog.wbgapi_enumerate import enumerate_wbgapi_indicators

    r = enumerate_wbgapi_indicators(session)
    assert r["imported"] == 0
    assert any("failed" in e for e in r["errors"])
    assert session.query(ConceptBinding).count() == 0  # nothing written
