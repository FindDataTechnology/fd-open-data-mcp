"""Tests for the path-based dict reader (fix-yfinance-catalog-import)."""
import pytest

from fd_open_data_mcp.catalog.readers import read_dict_by_path


def test_read_dict_by_path_loads_registry(tmp_path):
    f = tmp_path / "seed.py"
    f.write_text(
        "REGISTRY = {\n"
        "  'ticker_history': {'category': 'price', 'description': 'hist',\n"
        "    'parameters': [], 'columns': [{'name': 'Close', 'type': 'float'}]},\n"
        "}\n"
    )
    records, errors = read_dict_by_path(str(f), "REGISTRY", "upstream-curated")
    assert errors == []
    assert len(records) == 1
    assert records[0]["command"] == "ticker_history"
    assert records[0]["columns"][0]["name"] == "Close"
    assert records[0]["verified"] is True
    assert records[0]["scanner_mode"] == "upstream-curated"


def test_read_dict_by_path_missing(tmp_path):
    records, errors = read_dict_by_path(str(tmp_path / "nope.py"), "REGISTRY", "upstream-curated")
    assert records == []
    assert any("not found" in e for e in errors)


def test_read_dict_by_path_malformed(tmp_path):
    f = tmp_path / "bad.py"
    f.write_text("raise RuntimeError('boom')\n")
    records, errors = read_dict_by_path(str(f), "REGISTRY", "upstream-curated")
    assert records == []
    assert any("failed to load" in e for e in errors)


def test_import_yfinance_by_path(session):
    """Integration: import_provider('yfinance') reads seed.py by path (no fd_yfinance import)."""
    from fd_open_data_mcp.catalog.importer import import_provider
    from fd_open_data_mcp.catalog.providers import finddata_root

    seed = finddata_root() / "fd-yfinance" / "fd_yfinance" / "core" / "seed.py"
    if not seed.exists():
        pytest.skip("fd-yfinance seed.py not present")
    r = import_provider("yfinance", session)
    assert r["curated_count"] > 0
    assert r["errors"] == []
