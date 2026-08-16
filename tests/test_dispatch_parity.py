"""Task 2.7 — dispatch.py parity matrix across the 4 source families.

For each representative function — A-share (akshare ``stock_zh_a_hist``),
US (yfinance ``ticker_history``), macro (cnstats ``cpi``), EDGAR
(``company_get_filings``) — assert the adapter-delegated path through
dispatch.py's four helpers (``_build_params`` / ``_extract_value`` /
``_build_range_params`` / ``_extract_series``) produces the documented
per-source shape. This is the P1 Adapter Expansion acceptance gate: the
adapter is the authority, and dispatch.py delegates adapter-first (falling
back to legacy name-guessing only when no adapter is registered).

No network: each family fakes the upstream result in the shape its adapter
expects (akshare Chinese cols + 日期, yfinance date index + English cols,
cnstats 日期 + Chinese value cols, edgar CompanyFilings-like with
``.data.to_pandas()``).
"""
from datetime import date

import pandas as pd
import pytest

from fd_open_data_mcp import adapters
from fd_open_data_mcp.adapters import akshare as ak_mod
from fd_open_data_mcp.adapters import cnstats as cnstats_mod
from fd_open_data_mcp.adapters import edgar as edgar_mod
from fd_open_data_mcp.adapters import yfinance as yf_mod
from fd_open_data_mcp.fetch.dispatch import (
    _build_params,
    _build_range_params,
    _extract_series,
    _extract_value,
)
from fd_open_data_mcp.models import Function, FunctionColumn, Source


# --- shared helpers -----------------------------------------------------------

def _make_fn(session, source_name, command, parameters, column_name,
             column_type="float", source_label=None):
    """Seed a Source + Function + FunctionColumn matching an adapter's
    ``(source.name, command)`` registration key. Returns ``(fn, col)``."""
    src = session.query(Source).filter_by(name=source_name).first()
    if src is None:
        src = Source(name=source_name, label=source_label or source_name.title())
        session.add(src)
        session.flush()
    fn = session.query(Function).filter_by(source_id=src.id, command=command).first()
    if fn is None:
        fn = Function(
            source_id=src.id, command=command,
            parameters=parameters, verified=True, scanner_mode="upstream-curated",
        )
        session.add(fn)
        session.flush()
    col = FunctionColumn(function_id=fn.id, name=column_name, type=column_type)
    session.add(col)
    session.flush()
    session.commit()
    return fn, col


def _binding(col):
    """A binding-like stub: only ``.column.name`` is read by the adapters."""
    return type("_B", (), {"column": col})()


@pytest.fixture
def all_adapters():
    """Register every adapter module, then restore the prior registry state.

    Saves the registry, re-registers the real per-source adapters under test,
    and on teardown restores the saved state — so this fixture cannot pollute
    the global ``_REGISTRY`` for tests that run afterward (which rely on the
    adapter modules' import-time ``register_all()`` having populated the
    registry). Clear-only teardowns leave the registry empty and break later
    adapter-dependent tests; save/restore is non-polluting.
    """
    saved = dict(adapters._REGISTRY)
    adapters._REGISTRY.clear()
    ak_mod.register_all()
    yf_mod.register_all()
    cnstats_mod.register_all()
    edgar_mod.register_all()
    yield
    adapters._REGISTRY.clear()
    adapters._REGISTRY.update(saved)


class _FakePyarrowTable:
    """Stand-in for the pyarrow table inside an edgar ``CompanyFilings``."""

    def __init__(self, df):
        self._df = df

    def to_pandas(self):
        return self._df


class _FakeCompanyFilings:
    """Stand-in for edgar's ``CompanyFilings``: ``.data.to_pandas()`` -> DataFrame.

    ``_to_dataframe`` in the edgar adapter coerces via ``.data.to_pandas()``,
    so this fake exercises the SAME extraction code path a live
    ``CompanyFilings`` would (not a raw DataFrame passthrough).
    """

    def __init__(self, df):
        self.data = _FakePyarrowTable(df)


# =============================================================================
# A-share — akshare stock_zh_a_hist (Chinese cols + 日期, bare symbol, qfq)
# =============================================================================

def test_ashare_build_params(session, all_adapters):
    # stale registry params (start_time/end_time) are ignored by the adapter
    fn, col = _make_fn(
        session, "akshare", "stock_zh_a_hist",
        parameters=[{"name": "symbol"}, {"name": "start_time"}, {"name": "end_time"}],
        column_name="收盘",
    )
    params = _build_params(fn, "600000", "2024-07-26", _binding(col))
    assert params == {
        "symbol": "600000", "period": "daily",
        "start_date": "20240726", "end_date": "20240726",
        "adjust": "qfq", "timeout": 30.0,
    }


def test_ashare_extract_value(all_adapters):
    df = pd.DataFrame({
        "日期": [date(2024, 7, 25), date(2024, 7, 26)],
        "收盘": [11.0, 11.5],
        "开盘": [10.8, 11.0], "最高": [11.3, 11.8], "最低": [10.7, 10.9],
    })
    v = _extract_value(df, "收盘", "2024-07-26", source="akshare", command="stock_zh_a_hist")
    assert v == 11.5


def test_ashare_build_range_params(session, all_adapters):
    fn, col = _make_fn(
        session, "akshare", "stock_zh_a_hist",
        parameters=[{"name": "symbol"}, {"name": "start_time"}, {"name": "end_time"}],
        column_name="收盘",
    )
    params = _build_range_params(fn, "600000", "2024-07-26", "2024-07-28", _binding(col))
    # end_date overridden with _compact(end); start_date stays _compact(start)
    assert params == {
        "symbol": "600000", "period": "daily",
        "start_date": "20240726", "end_date": "20240728",
        "adjust": "qfq", "timeout": 30.0,
    }


def test_ashare_extract_series(all_adapters):
    df = pd.DataFrame({
        "日期": [date(2024, 7, 25), date(2024, 7, 26), date(2024, 7, 28), date(2024, 7, 29)],
        "收盘": [11.0, 11.5, 12.0, 12.5],
    })
    series = _extract_series(df, "收盘", "2024-07-26", "2024-07-28",
                             source="akshare", command="stock_zh_a_hist")
    assert series == {"2024-07-26": 11.5, "2024-07-28": 12.0}


# =============================================================================
# US — yfinance ticker_history (date INDEX + English cols, end exclusive)
# =============================================================================

def test_us_build_params(session, all_adapters):
    fn, col = _make_fn(
        session, "yfinance", "ticker_history",
        parameters=[{"name": "symbol"}, {"name": "start"}, {"name": "end"}],
        column_name="收盘",  # Chinese alias -> resolves to Close via _ALIASES
    )
    params = _build_params(fn, "AAPL", "2024-07-26", _binding(col))
    # end is exclusive -> advanced one day past the requested date
    assert params == {"symbol": "AAPL", "start": "2024-07-26", "end": "2024-07-27"}


def test_us_extract_value(all_adapters):
    idx = [pd.Timestamp("2024-07-25"), pd.Timestamp("2024-07-26")]
    df = pd.DataFrame({
        "Open": [10.0, 11.0], "High": [10.8, 11.8], "Low": [9.9, 10.9],
        "Close": [10.5, 11.5], "Volume": [100, 200], "Adj Close": [10.5, 11.5],
    }, index=idx)
    # 收盘 alias -> Close, picked off the date index
    v = _extract_value(df, "收盘", "2024-07-26", source="yfinance", command="ticker_history")
    assert v == 11.5


def test_us_build_range_params(session, all_adapters):
    fn, col = _make_fn(
        session, "yfinance", "ticker_history",
        parameters=[{"name": "symbol"}, {"name": "start"}, {"name": "end"}],
        column_name="收盘",
    )
    params = _build_range_params(fn, "AAPL", "2024-07-26", "2024-07-28", _binding(col))
    # end re-anchored to _next_day(end) so the range's last day is inclusive
    assert params == {"symbol": "AAPL", "start": "2024-07-26", "end": "2024-07-29"}


def test_us_extract_series(all_adapters):
    idx = [pd.Timestamp("2024-07-25"), pd.Timestamp("2024-07-26"),
           pd.Timestamp("2024-07-28"), pd.Timestamp("2024-07-29")]
    df = pd.DataFrame({"Close": [10.5, 11.5, 12.5, 13.0]}, index=idx)
    series = _extract_series(df, "收盘", "2024-07-26", "2024-07-28",
                             source="yfinance", command="ticker_history")
    assert series == {"2024-07-26": 11.5, "2024-07-28": 12.5}


# =============================================================================
# macro — cnstats cpi (KEYLESS: {} params, 日期 + Chinese value cols)
# =============================================================================

def test_macro_build_params(session, all_adapters):
    fn, col = _make_fn(
        session, "cnstats", "cpi",
        parameters=[],  # keyless — akshare macro functions take no args
        column_name="全国同比",
    )
    params = _build_params(fn, "CN", "2024-07-31", _binding(col))
    assert params == {}


def test_macro_extract_value(all_adapters):
    df = pd.DataFrame({
        "日期": [date(2024, 6, 30), date(2024, 7, 31), date(2024, 8, 31)],
        "全国同比": [0.2, 0.5, 0.6],
        "全国环比": [0.1, 0.3, 0.4],
    })
    v = _extract_value(df, "全国同比", "2024-07-31", source="cnstats", command="cpi")
    assert v == 0.5


def test_macro_build_range_params(session, all_adapters):
    fn, col = _make_fn(
        session, "cnstats", "cpi",
        parameters=[],
        column_name="全国同比",
    )
    params = _build_range_params(fn, "CN", "2024-06-30", "2024-08-31", _binding(col))
    assert params == {}


def test_macro_extract_series(all_adapters):
    df = pd.DataFrame({
        "日期": [date(2024, 6, 30), date(2024, 7, 31), date(2024, 8, 31), date(2024, 9, 30)],
        "全国同比": [0.2, 0.5, 0.6, 0.7],
    })
    series = _extract_series(df, "全国同比", "2024-07-31", "2024-08-31",
                             source="cnstats", command="cpi")
    assert series == {"2024-07-31": 0.5, "2024-08-31": 0.6}


# =============================================================================
# EDGAR — company_get_filings (ticker + filing_date, CompanyFilings shape)
# =============================================================================

def test_edgar_build_params(session, all_adapters):
    fn, col = _make_fn(
        session, "edgar", "company_get_filings",
        parameters=[{"name": "ticker"}, {"name": "filing_date"}],
        column_name="form", column_type="string",
    )
    params = _build_params(fn, "AAPL", "2024-02-23", _binding(col))
    assert params == {"ticker": "AAPL", "filing_date": "2024-02-23"}


def test_edgar_extract_value(all_adapters):
    df = pd.DataFrame({
        "filing_date": ["2024-01-15", "2024-02-23", "2024-03-30"],
        "form": ["10-Q", "10-K", "8-K"],
    })
    result = _FakeCompanyFilings(df)
    v = _extract_value(result, "form", "2024-02-23", source="edgar", command="company_get_filings")
    assert v == "10-K"


def test_edgar_build_range_params(session, all_adapters):
    fn, col = _make_fn(
        session, "edgar", "company_get_filings",
        parameters=[{"name": "ticker"}, {"name": "filing_date"}],
        column_name="form", column_type="string",
    )
    params = _build_range_params(fn, "AAPL", "2024-01-01", "2024-03-31", _binding(col))
    # filing_date becomes the "start:end" range form edgar's filter() accepts
    assert params == {"ticker": "AAPL", "filing_date": "2024-01-01:2024-03-31"}


def test_edgar_extract_series(all_adapters):
    df = pd.DataFrame({
        "filing_date": ["2023-12-31", "2024-01-15", "2024-02-23", "2024-03-30", "2024-04-15"],
        "form": ["10-K", "10-Q", "10-K", "8-K", "10-Q"],
    })
    result = _FakeCompanyFilings(df)
    series = _extract_series(result, "form", "2024-01-01", "2024-03-31",
                             source="edgar", command="company_get_filings")
    assert series == {"2024-01-15": "10-Q", "2024-02-23": "10-K", "2024-03-30": "8-K"}
