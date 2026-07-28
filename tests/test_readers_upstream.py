"""Tests for readers (read_db/dict/manifest/callable/mcp) + upstream introspection."""
import sqlite3
import sys
import types

import pytest

from fd_open_data_mcp.catalog.readers import (
    read_callable, read_db, read_dict, read_mcp, read_manifest,
)


def test_read_db(tmp_path):
    p = tmp_path / "reg.db"
    conn = sqlite3.connect(p)
    conn.execute("CREATE TABLE functions (id INTEGER, command TEXT, category TEXT, source TEXT, description TEXT, parameters TEXT)")
    conn.execute("CREATE TABLE function_columns (id INTEGER, function_id INTEGER, column_name TEXT, column_type TEXT, column_description TEXT)")
    conn.execute("INSERT INTO functions VALUES (1,'f','c','s','d','[]')")
    conn.execute("INSERT INTO function_columns VALUES (1,1,'close','float','close price')")
    conn.commit(); conn.close()
    recs, errs = read_db(str(p), "upstream-curated")
    assert errs == [] and len(recs) == 1
    assert recs[0]["command"] == "f"
    assert recs[0]["columns"][0]["name"] == "close"


def test_read_dict(monkeypatch):
    fake = types.ModuleType("fake_dict_mod")
    fake.REGISTRY = {"f": {"category": "c", "columns": [{"name": "close"}]}}
    monkeypatch.setitem(sys.modules, "fake_dict_mod", fake)
    recs, errs = read_dict("fake_dict_mod", "REGISTRY", "upstream-curated")
    assert errs == [] and recs[0]["command"] == "f"


def test_read_manifest(tmp_path):
    p = tmp_path / "manifest.db"
    conn = sqlite3.connect(p)
    conn.execute("CREATE TABLE sources (id INTEGER, name TEXT, label TEXT, url TEXT, description TEXT, category TEXT, category_label TEXT, config_json TEXT)")
    conn.execute("CREATE TABLE datasource_columns (id INTEGER, datasource_id INTEGER, table_name TEXT, column_name TEXT, column_type TEXT, description TEXT, source_field TEXT, unit TEXT, semantic_type TEXT)")
    conn.execute("INSERT INTO sources VALUES (1,'mee_archive','MEE','http://x','d','cat','Cat','{}')")
    conn.execute("INSERT INTO datasource_columns VALUES (1,1,'mee','title','string','title','a:text','','title')")
    conn.commit(); conn.close()
    recs, errs = read_manifest(str(p), "manifest-registry")
    assert errs == [] and recs[0]["command"] == "mee_archive"
    assert recs[0]["columns"][0]["semantic_type"] == "title"


def test_read_callable(monkeypatch):
    fake = types.ModuleType("fake_call_mod")
    fake.list_functions = lambda: [{"name": "f", "columns": [{"name": "close"}]}]
    monkeypatch.setitem(sys.modules, "fake_call_mod", fake)
    recs, errs = read_callable("fake_call_mod", "list_functions", "upstream-curated")
    assert errs == [] and recs[0]["command"] == "f"


def test_read_mcp(monkeypatch, tmp_path):
    fake_server = types.ModuleType("server")

    class FakeTool:
        name = "t1"
        description = "d"

    class FakeMCP:
        async def list_tools(self):
            return [FakeTool()]

    fake_server.mcp = FakeMCP()
    monkeypatch.setitem(sys.modules, "server", fake_server)
    recs, errs = read_mcp(str(tmp_path), "mcp-introspect")
    assert errs == [] and recs[0]["command"] == "t1"


def test_introspect_akshare(monkeypatch):
    fake = types.ModuleType("akshare")

    def foo():
        """doc"""

    fake.foo = foo
    monkeypatch.setitem(sys.modules, "akshare", fake)
    from fd_open_data_mcp.catalog.upstream import introspect_akshare
    recs = introspect_akshare()
    assert any(r["command"] == "foo" for r in recs)


def test_introspect_yfinance(monkeypatch):
    fake = types.ModuleType("yfinance")

    class Ticker:
        def history(self):
            """hist"""

    fake.Ticker = Ticker
    monkeypatch.setitem(sys.modules, "yfinance", fake)
    from fd_open_data_mcp.catalog.upstream import introspect_yfinance
    recs = introspect_yfinance()
    assert any(r["command"] == "ticker_history" for r in recs)


def test_introspect_edgar(monkeypatch):
    fake = types.ModuleType("edgar")

    class Company:
        def get_financials(self):
            """fin"""

    fake.Company = Company
    monkeypatch.setitem(sys.modules, "edgar", fake)
    from fd_open_data_mcp.catalog.upstream import introspect_edgar
    recs = introspect_edgar()
    assert any(r["command"] == "company_get_financials" for r in recs)


def test_introspect_wbgapi(monkeypatch):
    fake = types.ModuleType("wbgapi")

    class _Sub:
        def DataFrame(self, *a, **k):
            """df"""

    fake.data = _Sub()
    monkeypatch.setitem(sys.modules, "wbgapi", fake)
    from fd_open_data_mcp.catalog.upstream import introspect_wbgapi
    recs = introspect_wbgapi()
    assert any(r["command"] == "data_DataFrame" for r in recs)
