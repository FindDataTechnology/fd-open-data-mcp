"""Tests for the per-function adapter registry and dispatch delegation."""
from datetime import date

import pandas as pd
import pytest

from fd_open_data_mcp import adapters
from fd_open_data_mcp.adapters import akshare as ak_mod
from fd_open_data_mcp.adapters.akshare import (
    StockBalanceSheetAdapter,
    StockCashFlowSheetAdapter,
    StockFinancialIndicatorAdapter,
    StockProfitSheetAdapter,
    StockZhADailyAdapter,
    StockZhAHistAdapter,
    StockZhAHistTxAdapter,
)
from fd_open_data_mcp.adapters import yfinance as yf_mod
from fd_open_data_mcp.adapters.yfinance import TickerHistoryAdapter
from fd_open_data_mcp.adapters import edgar as edgar_mod
from fd_open_data_mcp.adapters.edgar import CompanyGetFilingsAdapter
from fd_open_data_mcp.adapters import edinet as edinet_mod
from fd_open_data_mcp.adapters.edinet import EntityDocumentsAdapter
from fd_open_data_mcp.adapters import dartlab as dartlab_mod
from fd_open_data_mcp.adapters.dartlab import (
    CompanyAnalysisAdapter,
    CompanyCreditAdapter,
    CompanyDisclosureAdapter,
    CompanyNewsAdapter,
    CompanyPanelAdapter,
    CompanySearchAdapter,
)
from fd_open_data_mcp.adapters import ckan as ckan_mod
from fd_open_data_mcp.adapters.ckan import (
    OrganizationListAdapter,
    PackageSearchAdapter,
    PackageShowAdapter,
    ResourceShowAdapter,
    TagListAdapter,
)
from fd_open_data_mcp.adapters import cnstats as cnstats_mod
from fd_open_data_mcp.adapters.cnstats import MAPPING as CNSTATS_MAPPING, _CnstatsBase
from fd_open_data_mcp.fetch.dispatch import _build_params, _extract_value
from fd_open_data_mcp.models import Function, FunctionColumn, Source


class _DummyAdapter:
    """An adapter that maps by position (not name-guessing) to prove delegation."""

    def build_params(self, fn, identifier, date, binding):
        return {"symbol": identifier, "the_date": date, "indicator": binding.column.name}

    def extract_value(self, result, column_name, date, identifier=None):
        # deliberate non-legacy extraction: read by position, not date-index
        # (identifier is unused here; it's part of the Adapter Protocol so
        # rank-frame adapters can pick their row, but this dummy reads row 0.)
        if isinstance(result, pd.DataFrame) and len(result) > 0:
            return ("adapter", result.iloc[0, 0], column_name, date)
        return None


def _make_function(session, command="get_hist", source_name="test-src"):
    src = session.query(Source).filter_by(name=source_name).first()
    if src is None:
        src = Source(name=source_name, label="Test Src")
        session.add(src)
        session.flush()
    fn = session.query(Function).filter_by(source_id=src.id, command=command).first()
    if fn is None:
        fn = Function(
            source_id=src.id, command=command,
            parameters=[{"name": "symbol"}, {"name": "date"}, {"name": "indicator"}],
            verified=True,
        )
        session.add(fn)
        session.flush()
    col = FunctionColumn(function_id=fn.id, name="close", type="float")
    session.add(col)
    session.flush()
    session.commit()
    return fn, col


def test_registry_register_and_lookup():
    adapters._REGISTRY.clear()
    a = _DummyAdapter()
    assert adapters.adapter_for("akshare", "stock_zh_a_hist") is None
    assert not adapters.has_adapter("akshare", "stock_zh_a_hist")

    adapters.register("akshare", "stock_zh_a_hist", a)
    assert adapters.adapter_for("akshare", "stock_zh_a_hist") is a
    assert adapters.has_adapter("akshare", "stock_zh_a_hist")
    assert ("akshare", "stock_zh_a_hist") in adapters.registered()


def test_build_params_delegates_to_adapter(session):
    adapters._REGISTRY.clear()
    fn, col = _make_function(session)
    # a binding-like stub: only .column.name is used by the dummy adapter
    class _B:
        column = col
    binding = _B()

    # no adapter -> legacy best-effort (name-guessing: symbol + date mapped)
    params = _build_params(fn, "600000", "2024-07-26", binding)
    assert params == {"symbol": "600000", "date": "2024-07-26", "indicator": "close"}

    # register adapter -> delegation (note: "the_date", not "date")
    adapters.register("test-src", "get_hist", _DummyAdapter())
    params = _build_params(fn, "600000", "2024-07-26", binding)
    assert params == {"symbol": "600000", "the_date": "2024-07-26", "indicator": "close"}
    adapters._REGISTRY.clear()


def test_extract_value_delegates_to_adapter(session):
    adapters._REGISTRY.clear()
    fn, col = _make_function(session)
    df = pd.DataFrame({"close": [12.34]}, index=["2024-07-26"])

    # no adapter, no source/command -> legacy DataFrame lookup
    v = _extract_value(df, "close", "2024-07-26")
    assert v == 12.34

    # adapter registered -> delegation (returns the adapter's tuple, not the scalar)
    adapters.register("test-src", "get_hist", _DummyAdapter())
    v = _extract_value(df, "close", "2024-07-26", source="test-src", command="get_hist")
    assert v == ("adapter", 12.34, "close", "2024-07-26")

    # adapter registered but source/command not passed -> legacy fallback
    v = _extract_value(df, "close", "2024-07-26")
    assert v == 12.34
    adapters._REGISTRY.clear()


# ---------------------------------------------------------------------------
# akshare adapters (ported from scraw-akshare) - tasks 2.2/2.3
# ---------------------------------------------------------------------------

_AKSHARE_COMMANDS = [
    "stock_zh_a_hist", "stock_zh_a_hist_tx", "stock_zh_a_daily",
    "stock_financial_analysis_indicator", "stock_profit_sheet_by_report_em",
    "stock_balance_sheet_by_report_em", "stock_cash_flow_sheet_by_report_em",
]


def _register_akshare():
    """Re-populate the registry with the built-in akshare adapters.

    The earlier tests ``clear()`` the shared registry; calling ``register_all()``
    re-runs the registrations without redefining the classes (which would break
    ``isinstance`` the way ``importlib.reload`` does).
    """
    ak_mod.register_all()
    return ak_mod


def test_akshare_adapters_registered():
    adapters._REGISTRY.clear()
    _register_akshare()
    keys = adapters.registered()
    for cmd in _AKSHARE_COMMANDS:
        assert ("akshare", cmd) in keys
        assert adapters.has_adapter("akshare", cmd)
        assert adapters.adapter_for("akshare", cmd) is not None
    assert isinstance(adapters.adapter_for("akshare", "stock_zh_a_hist"), StockZhAHistAdapter)
    adapters._REGISTRY.clear()


# --- daily OHLCV: stock_zh_a_hist (East Money, Chinese columns) ----------------

def test_stock_zh_a_hist_build_params():
    a = StockZhAHistAdapter()
    params = a.build_params(None, "600000", "2024-07-26", None)
    # bare symbol (no exchange prefix), YYYYMMDD date range, qfq, native timeout
    assert params == {
        "symbol": "600000", "period": "daily",
        "start_date": "20240726", "end_date": "20240726",
        "adjust": "qfq", "timeout": 30.0,
    }


def test_stock_zh_a_hist_extract_value():
    df = pd.DataFrame({
        "日期": [date(2024, 7, 25), date(2024, 7, 26)],
        "股票代码": ["600000", "600000"],
        "开盘": [10.0, 11.0], "收盘": [10.5, 11.5],
        "最高": [10.8, 11.8], "最低": [9.9, 10.9],
        "成交量": [100.0, 200.0], "成交额": [1000.0, 2000.0],
        "振幅": [0.0, 0.0], "涨跌幅": [0.0, 0.0], "涨跌额": [0.0, 0.0], "换手率": [0.0, 0.0],
    })
    a = StockZhAHistAdapter()
    assert a.extract_value(df, "收盘", "2024-07-26") == 11.5
    # date-format tolerance: request YYYYMMDD against a date-object column
    assert a.extract_value(df, "收盘", "20240726") == 11.5
    assert a.extract_value(df, "收盘", "2024-07-25") == 10.5
    assert a.extract_value(df, "开盘", "2024-07-26") == 11.0
    # missing date / missing column -> None
    assert a.extract_value(df, "收盘", "2024-01-01") is None
    assert a.extract_value(df, "nope", "2024-07-26") is None
    # a YYYY-MM-DD string column is also tolerated
    df_str = df.copy()
    df_str["日期"] = ["2024-07-25", "2024-07-26"]
    assert a.extract_value(df_str, "收盘", "2024-07-26") == 11.5


# --- daily OHLCV: stock_zh_a_hist_tx (Tencent, English columns) ----------------

def test_stock_zh_a_hist_tx_build_params():
    a = StockZhAHistTxAdapter()
    # 6 -> sh prefix; Tencent symbol is prefixed; no `period` (always daily)
    params = a.build_params(None, "600000", "2024-07-26", None)
    assert params == {
        "symbol": "sh600000", "start_date": "20240726", "end_date": "20240726",
        "adjust": "qfq", "timeout": 30.0,
    }
    # 0 -> sz prefix
    assert a.build_params(None, "000001", "2024-07-26", None)["symbol"] == "sz000001"
    # 8 -> bj prefix
    assert a.build_params(None, "830007", "2024-07-26", None)["symbol"] == "bj830007"


def test_stock_zh_a_hist_tx_extract_value():
    df = pd.DataFrame({
        "date": ["2024-07-26", "2024-07-25"],
        "open": [11.0, 10.0], "high": [11.8, 10.8], "low": [10.9, 9.9],
        "close": [11.5, 10.5], "amount": [2000.0, 1000.0],
    })
    a = StockZhAHistTxAdapter()
    # concept bound to the Chinese name 收盘 must resolve via alias to `close`
    assert a.extract_value(df, "收盘", "2024-07-26") == 11.5
    # and the physical English name works too
    assert a.extract_value(df, "close", "2024-07-26") == 11.5
    assert a.extract_value(df, "成交额", "2024-07-26") == 2000.0
    assert a.extract_value(df, "收盘", "2024-01-01") is None


# --- daily OHLCV: stock_zh_a_daily (Sina, English columns, sina prefix) --------

def test_stock_zh_a_daily_build_params():
    a = StockZhADailyAdapter()
    params = a.build_params(None, "600000", "2024-07-26", None)
    # sina prefix (6 -> sh), NO timeout kwarg (sina endpoint lacks one)
    assert params == {
        "symbol": "sh600000", "start_date": "20240726", "end_date": "20240726",
        "adjust": "qfq",
    }
    assert "timeout" not in params
    # 9 -> sh (sina B-share), 8 -> bj, 0 -> sz
    assert a.build_params(None, "900901", "2024-07-26", None)["symbol"] == "sh900901"
    assert a.build_params(None, "000001", "2024-07-26", None)["symbol"] == "sz000001"


def test_stock_zh_a_daily_extract_value():
    # akshare returns `date` as a column of python date objects
    df = pd.DataFrame({
        "date": [date(2024, 7, 26)], "open": [11.0], "high": [11.8],
        "low": [10.9], "close": [11.5], "volume": [200.0],
        "amount": [2000.0], "outstanding_share": [1e7], "turnover": [0.0],
    })
    a = StockZhADailyAdapter()
    assert a.extract_value(df, "收盘", "2024-07-26") == 11.5
    assert a.extract_value(df, "close", "2024-07-26") == 11.5


# --- financials --------------------------------------------------------------

def test_stock_financial_indicator_adapter():
    a = StockFinancialIndicatorAdapter()
    # bare symbol; start_year derived from the requested date's year
    assert a.build_params(None, "600000", "2024-06-30", None) == {
        "symbol": "600000", "start_year": "2024",
    }
    df = pd.DataFrame({"日期": ["2024-06-30"], "净资产收益率(%)": [15.3]})
    assert a.extract_value(df, "净资产收益率(%)", "2024-06-30") == 15.3
    assert a.extract_value(df, "净资产收益率(%)", "2024-03-31") is None


@pytest.mark.parametrize("cls,command", [
    (StockProfitSheetAdapter, "stock_profit_sheet_by_report_em"),
    (StockBalanceSheetAdapter, "stock_balance_sheet_by_report_em"),
    (StockCashFlowSheetAdapter, "stock_cash_flow_sheet_by_report_em"),
])
def test_em_statement_adapters(cls, command):
    _register_akshare()
    a = cls()
    # em statement endpoints take only `symbol` (bare code); they return ALL
    # report periods, so the requested date is matched at extract time on 报告期.
    assert a.build_params(None, "600000", "2024-06-30", None) == {"symbol": "600000"}
    assert a._DATE_COL == "报告期"
    df = pd.DataFrame({"报告期": ["2024-06-30", "2024-03-31"], "净利润": [1.2e9, 9e8]})
    assert a.extract_value(df, "净利润", "2024-06-30") == 1.2e9
    assert a.extract_value(df, "净利润", "2024-03-31") == 9e8
    assert a.extract_value(df, "净利润", "2024-12-31") is None
    # and the adapter class is registered under the right command
    assert isinstance(adapters.adapter_for("akshare", command), cls)
    adapters._REGISTRY.clear()


# --- dispatch delegation to the REAL akshare adapter ---------------------------

def _make_akshare_function(session, command="stock_zh_a_hist"):
    src = session.query(Source).filter_by(name="akshare").first()
    if src is None:
        src = Source(name="akshare", label="AKShare")
        session.add(src)
        session.flush()
    fn = session.query(Function).filter_by(source_id=src.id, command=command).first()
    if fn is None:
        # stale registry parameters (start_time/end_time) - the adapter ignores them
        fn = Function(
            source_id=src.id, command=command,
            parameters=[{"name": "symbol"}, {"name": "start_time"}, {"name": "end_time"}],
            verified=True, scanner_mode="upstream-curated",
        )
        session.add(fn)
        session.flush()
    col = FunctionColumn(function_id=fn.id, name="收盘", type="float")
    session.add(col)
    session.flush()
    session.commit()
    return fn, col


def test_build_params_delegates_to_real_akshare_adapter(session):
    adapters._REGISTRY.clear()
    _register_akshare()
    fn, col = _make_akshare_function(session)
    binding = type("_B", (), {"column": col})()

    # adapter is registered -> delegation; result is the adapter's REAL mapping
    # (bare symbol, YYYYMMDD dates, qfq, timeout), NOT the legacy name-guessing
    # that would have produced {symbol, start_time, end_time}.
    params = _build_params(fn, "600000", "2024-07-26", binding)
    assert params == {
        "symbol": "600000", "period": "daily",
        "start_date": "20240726", "end_date": "20240726",
        "adjust": "qfq", "timeout": 30.0,
    }
    adapters._REGISTRY.clear()


def test_extract_value_delegates_to_real_akshare_adapter(session):
    adapters._REGISTRY.clear()
    _register_akshare()
    df = pd.DataFrame({
        "日期": [date(2024, 7, 26)], "收盘": [11.5],
        "开盘": [11.0], "最高": [11.8], "最低": [10.9],
    })
    # passing source+command -> adapter delegation pulls the right Chinese column
    v = _extract_value(df, "收盘", "2024-07-26", source="akshare", command="stock_zh_a_hist")
    assert v == 11.5
    adapters._REGISTRY.clear()


# --- task 2.3: call() retry + timeout, and run_upstream delegation (no network) -

class _FakeAkshare:
    """A fake `akshare` module whose command raises/returns per a script."""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[dict] = []

    def stock_zh_a_hist(self, **params):
        self.calls.append(params)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_adapter_call_retries_then_succeeds(monkeypatch):
    import sys

    fake = _FakeAkshare([RuntimeError("boom"), RuntimeError("boom"), "OK"])
    monkeypatch.setitem(sys.modules, "akshare", fake)
    a = StockZhAHistAdapter()
    a._RETRIES = 2
    a._RETRY_DELAY = 0  # don't actually sleep in tests
    params = a.build_params(None, "600000", "2024-07-26", None)

    result = a.call("stock_zh_a_hist", params)
    assert result == "OK"
    assert len(fake.calls) == 3                      # 2 failures + 1 success
    assert fake.calls[-1]["timeout"] == 30.0          # native timeout kwarg passed through


def test_adapter_call_raises_fetcherror_after_exhausting_retries(monkeypatch):
    import sys

    from fd_open_data_mcp.fetch.runner import FetchError

    fake = _FakeAkshare([RuntimeError("boom")] * 3)
    monkeypatch.setitem(sys.modules, "akshare", fake)
    a = StockZhAHistAdapter()
    a._RETRIES = 2
    a._RETRY_DELAY = 0

    with pytest.raises(FetchError):
        a.call("stock_zh_a_hist", a.build_params(None, "600000", "2024-07-26", None))
    assert len(fake.calls) == 3


def test_run_upstream_delegates_to_adapter_call(monkeypatch):
    """run_akshare opts into the adapter's call() (retry) when one is registered."""
    import sys

    from fd_open_data_mcp.fetch.runner import run_upstream

    fake = _FakeAkshare([RuntimeError("transient"), "OK-DATAFRAME"])
    monkeypatch.setitem(sys.modules, "akshare", fake)
    a = StockZhAHistAdapter()
    a._RETRIES = 1
    a._RETRY_DELAY = 0
    adapters.register("akshare", "stock_zh_a_hist", a)
    try:
        out = run_upstream("akshare", "stock_zh_a_hist",
                           a.build_params(None, "600000", "2024-07-26", None))
        assert out == "OK-DATAFRAME"
        assert len(fake.calls) == 2                 # 1 failure retried, then success
    finally:
        adapters._REGISTRY.clear()


def test_run_upstream_legacy_path_when_no_adapter(monkeypatch):
    """With no adapter registered, run_akshare falls back to a direct call."""
    import sys

    from fd_open_data_mcp.fetch.runner import run_upstream

    fake = _FakeAkshare(["DIRECT-OK"])
    monkeypatch.setitem(sys.modules, "akshare", fake)
    adapters._REGISTRY.clear()
    out = run_upstream("akshare", "stock_zh_a_hist", {"symbol": "600000"})
    assert out == "DIRECT-OK"
    assert len(fake.calls) == 1                     # no retry on the legacy path


# ---------------------------------------------------------------------------
# yfinance adapter (task 2.1) - Ticker.history returns a date-indexed DataFrame
# ---------------------------------------------------------------------------

class _FakeYfinance:
    """A fake `yfinance` module: ``Ticker(symbol).history(**kwargs)`` raises/returns
    per a script. Records every call's kwargs in ``_ticker.calls``."""

    def __init__(self, script):
        self._ticker = self._FakeTicker(script)

    def Ticker(self, symbol):
        self._ticker.symbol = symbol
        return self._ticker

    class _FakeTicker:
        def __init__(self, script):
            self.script = list(script)
            self.calls: list[dict] = []
            self.symbol = None

        def history(self, **kwargs):
            self.calls.append(kwargs)
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item


def test_yfinance_adapter_registered():
    adapters._REGISTRY.clear()
    yf_mod.register_all()
    assert adapters.has_adapter("yfinance", "ticker_history")
    assert isinstance(adapters.adapter_for("yfinance", "ticker_history"), TickerHistoryAdapter)
    adapters._REGISTRY.clear()


def test_yfinance_adapter_build_params():
    a = TickerHistoryAdapter()
    # single date -> start=date, end=_next_day(date) (yfinance end is exclusive)
    params = a.build_params(None, "AAPL", "2024-07-26", None)
    assert params == {"symbol": "AAPL", "start": "2024-07-26", "end": "2024-07-27"}
    # tolerates an 8-digit YYYYMMDD requested date
    params = a.build_params(None, "AAPL", "20240726", None)
    assert params == {"symbol": "AAPL", "start": "2024-07-26", "end": "2024-07-27"}


def test_yfinance_adapter_build_range_params():
    a = TickerHistoryAdapter()
    # range form: start as-is, end advanced one day past the range's last day
    params = a.build_range_params(None, "AAPL", "2024-07-26", "2024-07-28", None)
    assert params == {"symbol": "AAPL", "start": "2024-07-26", "end": "2024-07-29"}


def test_yfinance_adapter_extract_value():
    # yfinance returns a DataFrame INDEXED BY DATE with English columns
    idx = [pd.Timestamp("2024-07-25"), pd.Timestamp("2024-07-26")]
    df = pd.DataFrame({
        "Open": [10.0, 11.0], "High": [10.8, 11.8], "Low": [9.9, 10.9],
        "Close": [10.5, 11.5], "Volume": [100, 200], "Adj Close": [10.5, 11.5],
    }, index=idx)
    a = TickerHistoryAdapter()
    # English column name
    assert a.extract_value(df, "Close", "2024-07-26") == 11.5
    assert a.extract_value(df, "Close", "2024-07-25") == 10.5
    # Chinese alias resolves to Close
    assert a.extract_value(df, "收盘", "2024-07-26") == 11.5
    assert a.extract_value(df, "开盘", "2024-07-26") == 11.0
    # date-format tolerance: request YYYYMMDD against a Timestamp index
    assert a.extract_value(df, "Close", "20240726") == 11.5
    # missing date / missing column -> None
    assert a.extract_value(df, "Close", "2024-01-01") is None
    assert a.extract_value(df, "nope", "2024-07-26") is None


def test_yfinance_adapter_extract_series():
    idx = [pd.Timestamp("2024-07-25"), pd.Timestamp("2024-07-26"), pd.Timestamp("2024-07-29")]
    df = pd.DataFrame({"Close": [10.5, 11.5, 12.5]}, index=idx)
    a = TickerHistoryAdapter()
    # batch form: every (date, Close) with start <= date <= end
    out = a.extract_series(df, "Close", "2024-07-26", "2024-07-28")
    assert out == {"2024-07-26": 11.5}  # 07-25 before start, 07-29 after end
    out = a.extract_series(df, "收盘", "2024-07-25", "2024-07-29")
    assert out == {"2024-07-25": 10.5, "2024-07-26": 11.5, "2024-07-29": 12.5}


def test_yfinance_adapter_call_retries_then_succeeds(monkeypatch):
    import sys

    fake = _FakeYfinance([RuntimeError("boom"), RuntimeError("boom"), "OK"])
    monkeypatch.setitem(sys.modules, "yfinance", fake)
    a = TickerHistoryAdapter()
    a._RETRIES = 2
    a._RETRY_DELAY = 0  # don't actually sleep in tests
    params = a.build_params(None, "AAPL", "2024-07-26", None)

    result = a.call("ticker_history", params)
    assert result == "OK"
    assert len(fake._ticker.calls) == 3               # 2 failures + 1 success
    # date-range kwargs passed through to Ticker.history
    assert fake._ticker.calls[-1]["start"] == "2024-07-26"
    assert fake._ticker.calls[-1]["end"] == "2024-07-27"  # _next_day
    assert fake._ticker.symbol == "AAPL"


def test_yfinance_adapter_call_raises_fetcherror_after_exhausting_retries(monkeypatch):
    import sys

    from fd_open_data_mcp.fetch.runner import FetchError

    fake = _FakeYfinance([RuntimeError("boom")] * 3)
    monkeypatch.setitem(sys.modules, "yfinance", fake)
    a = TickerHistoryAdapter()
    a._RETRIES = 2
    a._RETRY_DELAY = 0

    with pytest.raises(FetchError):
        a.call("ticker_history", a.build_params(None, "AAPL", "2024-07-26", None))
    assert len(fake._ticker.calls) == 3


def test_yfinance_adapter_call_missing_symbol_raises(monkeypatch):
    import sys

    from fd_open_data_mcp.fetch.runner import FetchError

    fake = _FakeYfinance(["OK"])
    monkeypatch.setitem(sys.modules, "yfinance", fake)
    a = TickerHistoryAdapter()
    with pytest.raises(FetchError):
        a.call("ticker_history", {"start": "2024-07-26", "end": "2024-07-27"})


def test_run_upstream_delegates_to_yfinance_adapter_call(monkeypatch):
    """run_yfinance opts into the adapter's call() (retry) when one is registered."""
    import sys

    from fd_open_data_mcp.fetch.runner import run_upstream

    fake = _FakeYfinance([RuntimeError("transient"), "OK-DATAFRAME"])
    monkeypatch.setitem(sys.modules, "yfinance", fake)
    a = TickerHistoryAdapter()
    a._RETRIES = 1
    a._RETRY_DELAY = 0
    adapters.register("yfinance", "ticker_history", a)
    try:
        out = run_upstream("yfinance", "ticker_history",
                           a.build_params(None, "AAPL", "2024-07-26", None))
        assert out == "OK-DATAFRAME"
        assert len(fake._ticker.calls) == 2          # 1 failure retried, then success
    finally:
        adapters._REGISTRY.clear()


def test_run_upstream_yfinance_legacy_path_when_no_adapter(monkeypatch):
    """With no adapter registered, run_yfinance falls back to the legacy direct call
    (symbol popped, ``ticker.<method>()`` invoked with NO kwargs)."""
    import sys

    from fd_open_data_mcp.fetch.runner import run_upstream

    fake = _FakeYfinance(["DIRECT-OK"])
    monkeypatch.setitem(sys.modules, "yfinance", fake)
    adapters._REGISTRY.clear()
    out = run_upstream("yfinance", "ticker_history", {"symbol": "AAPL"})
    assert out == "DIRECT-OK"
    assert len(fake._ticker.calls) == 1              # no retry on the legacy path
    # legacy path passes NO kwargs to .history()
    assert fake._ticker.calls[0] == {}


# ---------------------------------------------------------------------------
# edgar adapter (task 2.2) - Company.get_filings returns a CompanyFilings
# object (NOT a DataFrame); .data is a pyarrow table, .filter(filing_date=...)
# narrows by date. The adapter coerces to a DataFrame at extract time.
# ---------------------------------------------------------------------------

class _PyarrowLike:
    """Fake pyarrow table: ``.to_pandas()`` returns the wrapped DataFrame."""

    def __init__(self, df):
        self._df = df

    def to_pandas(self):
        return self._df


class _FakeCompanyFilings:
    """Fake edgar ``CompanyFilings``: ``.data.to_pandas()`` -> DataFrame;
    ``.filter(filing_date=...)`` narrows by date (single date or ``start:end``)."""

    def __init__(self, df):
        self._df = df
        self.data = _PyarrowLike(df)
        self.filter_calls: list[dict] = []

    def filter(self, **kwargs):
        self.filter_calls.append(kwargs)
        fd = kwargs.get("filing_date") or kwargs.get("date")
        df = self._df
        if fd:
            if ":" in fd:
                start, end = fd.split(":")
                if start:
                    df = df[df["filing_date"] >= start]
                if end:
                    df = df[df["filing_date"] <= end]
            else:
                df = df[df["filing_date"] == fd]
        return _FakeCompanyFilings(df.reset_index(drop=True))


class _FakeCompany:
    """Fake ``edgar.Company``: ``get_filings()`` raises/returns per a script."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.ticker = None

    def get_filings(self):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _FakeEdgar:
    """Fake ``edgar`` module: ``set_identity`` + ``Company`` factory."""

    def __init__(self, script):
        self._company = _FakeCompany(script)
        self.identity_calls: list[str] = []

    def set_identity(self, identity):
        self.identity_calls.append(identity)

    def Company(self, ticker):
        self._company.ticker = ticker
        return self._company


def _edgar_filings_df():
    return pd.DataFrame({
        "filing_date": ["2024-07-25", "2024-07-26", "2024-07-29"],
        "form": ["10-K", "10-Q", "8-K"],
        "accession_no": ["000-1", "000-2", "000-3"],
    })


def test_edgar_adapter_registered():
    adapters._REGISTRY.clear()
    edgar_mod.register_all()
    assert adapters.has_adapter("edgar", "company_get_filings")
    assert isinstance(adapters.adapter_for("edgar", "company_get_filings"), CompanyGetFilingsAdapter)
    adapters._REGISTRY.clear()


def test_edgar_adapter_build_params():
    a = CompanyGetFilingsAdapter()
    # single date -> ticker + filing_date (normalized ISO)
    params = a.build_params(None, "AAPL", "2024-07-26", None)
    assert params == {"ticker": "AAPL", "filing_date": "2024-07-26"}
    # tolerates an 8-digit YYYYMMDD requested date
    params = a.build_params(None, "AAPL", "20240726", None)
    assert params == {"ticker": "AAPL", "filing_date": "2024-07-26"}


def test_edgar_adapter_build_range_params():
    a = CompanyGetFilingsAdapter()
    # range form: filing_date becomes "start:end"
    params = a.build_range_params(None, "AAPL", "2024-07-26", "2024-07-28", None)
    assert params == {"ticker": "AAPL", "filing_date": "2024-07-26:2024-07-28"}


def test_edgar_adapter_extract_value():
    filings = _FakeCompanyFilings(_edgar_filings_df())
    a = CompanyGetFilingsAdapter()
    # pull a column cell by filing date
    assert a.extract_value(filings, "form", "2024-07-26") == "10-Q"
    assert a.extract_value(filings, "form", "2024-07-25") == "10-K"
    assert a.extract_value(filings, "accession_no", "2024-07-29") == "000-3"
    # date-format tolerance: request YYYYMMDD against a 'YYYY-MM-DD' column
    assert a.extract_value(filings, "form", "20240726") == "10-Q"
    # missing date / missing column -> None
    assert a.extract_value(filings, "form", "2024-01-01") is None
    assert a.extract_value(filings, "nope", "2024-07-26") is None


def test_edgar_adapter_extract_value_tolerates_filingDate_column():
    # edgartools has renamed filing_date -> filingDate across versions; the
    # adapter probes candidate date-column names rather than hardcoding one.
    a = CompanyGetFilingsAdapter()
    df = pd.DataFrame({
        "filingDate": ["2024-07-25", "2024-07-26"],
        "form": ["10-K", "10-Q"],
    })
    filings = _FakeCompanyFilings(df)
    assert a.extract_value(filings, "form", "2024-07-26") == "10-Q"


def test_edgar_adapter_extract_value_plain_dataframe():
    # _to_dataframe is a no-op for an already-DataFrame result (defensive path
    # for callers that hand extract_value a frame directly, not a CompanyFilings).
    a = CompanyGetFilingsAdapter()
    df = _edgar_filings_df()
    assert a.extract_value(df, "form", "2024-07-26") == "10-Q"
    assert a.extract_value(df, "form", "2024-07-25") == "10-K"


def test_edgar_adapter_extract_series():
    filings = _FakeCompanyFilings(_edgar_filings_df())
    a = CompanyGetFilingsAdapter()
    # batch form: every (filing_date, form) with start <= date <= end
    out = a.extract_series(filings, "form", "2024-07-26", "2024-07-28")
    assert out == {"2024-07-26": "10-Q"}  # 07-25 before start, 07-29 after end
    out = a.extract_series(filings, "form", "2024-07-25", "2024-07-29")
    assert out == {"2024-07-25": "10-K", "2024-07-26": "10-Q", "2024-07-29": "8-K"}


def test_edgar_adapter_call_retries_then_succeeds(monkeypatch):
    import sys

    from fd_open_data_mcp.fetch import runner

    filings_ok = _FakeCompanyFilings(pd.DataFrame({
        "filing_date": ["2024-07-26"], "form": ["10-Q"],
    }))
    fake = _FakeEdgar([RuntimeError("boom"), RuntimeError("boom"), filings_ok])
    monkeypatch.setitem(sys.modules, "edgar", fake)
    monkeypatch.setenv("EDGAR_IDENTITY", "test@example.com")
    runner._EDGAR_IDENTITY_SET = False

    a = CompanyGetFilingsAdapter()
    a._RETRIES = 2
    a._RETRY_DELAY = 0  # don't actually sleep in tests
    params = a.build_params(None, "AAPL", "2024-07-26", None)

    result = a.call("company_get_filings", params)
    # result is the *filtered* CompanyFilings; verify via extract_value
    assert a.extract_value(result, "form", "2024-07-26") == "10-Q"
    assert fake._company.calls == 3               # 2 failures + 1 success
    assert fake.identity_calls == ["test@example.com"]  # SEC identity set once
    # filter called with the requested filing_date
    assert filings_ok.filter_calls == [{"filing_date": "2024-07-26"}]
    assert fake._company.ticker == "AAPL"


def test_edgar_adapter_call_raises_fetcherror_after_exhausting_retries(monkeypatch):
    import sys

    from fd_open_data_mcp.fetch import runner
    from fd_open_data_mcp.fetch.runner import FetchError

    fake = _FakeEdgar([RuntimeError("boom")] * 3)
    monkeypatch.setitem(sys.modules, "edgar", fake)
    monkeypatch.setenv("EDGAR_IDENTITY", "test@example.com")
    runner._EDGAR_IDENTITY_SET = False
    a = CompanyGetFilingsAdapter()
    a._RETRIES = 2
    a._RETRY_DELAY = 0

    with pytest.raises(FetchError):
        a.call("company_get_filings", a.build_params(None, "AAPL", "2024-07-26", None))
    assert fake._company.calls == 3


def test_edgar_adapter_call_missing_ticker_raises(monkeypatch):
    import sys

    from fd_open_data_mcp.fetch import runner
    from fd_open_data_mcp.fetch.runner import FetchError

    fake = _FakeEdgar(["unused"])
    monkeypatch.setitem(sys.modules, "edgar", fake)
    monkeypatch.setenv("EDGAR_IDENTITY", "test@example.com")
    runner._EDGAR_IDENTITY_SET = False
    a = CompanyGetFilingsAdapter()
    with pytest.raises(FetchError):
        a.call("company_get_filings", {"filing_date": "2024-07-26"})


def test_edgar_adapter_call_raises_when_identity_unset(monkeypatch):
    """No EDGAR_IDENTITY -> FetchError before any upstream call (SEC requirement)."""
    import sys

    from fd_open_data_mcp.fetch import runner
    from fd_open_data_mcp.fetch.runner import FetchError

    fake = _FakeEdgar(["unused"])
    monkeypatch.setitem(sys.modules, "edgar", fake)
    monkeypatch.delenv("EDGAR_IDENTITY", raising=False)
    runner._EDGAR_IDENTITY_SET = False
    a = CompanyGetFilingsAdapter()
    with pytest.raises(FetchError):
        a.call("company_get_filings", {"ticker": "AAPL", "filing_date": "2024-07-26"})
    assert fake._company.calls == 0  # never reached the upstream call


def test_run_upstream_delegates_to_edgar_adapter_call(monkeypatch):
    """run_edgar opts into the adapter's call() (retry + filter) when registered."""
    import sys

    from fd_open_data_mcp.fetch import runner
    from fd_open_data_mcp.fetch.runner import run_upstream

    filings_ok = _FakeCompanyFilings(pd.DataFrame({
        "filing_date": ["2024-07-26"], "form": ["10-Q"],
    }))
    fake = _FakeEdgar([RuntimeError("transient"), filings_ok])
    monkeypatch.setitem(sys.modules, "edgar", fake)
    monkeypatch.setenv("EDGAR_IDENTITY", "test@example.com")
    runner._EDGAR_IDENTITY_SET = False
    a = CompanyGetFilingsAdapter()
    a._RETRIES = 1
    a._RETRY_DELAY = 0
    adapters.register("edgar", "company_get_filings", a)
    try:
        out = run_upstream("edgar", "company_get_filings",
                           a.build_params(None, "AAPL", "2024-07-26", None))
        assert a.extract_value(out, "form", "2024-07-26") == "10-Q"
        assert fake._company.calls == 2          # 1 failure retried, then success
    finally:
        adapters._REGISTRY.clear()


def test_run_upstream_edgar_legacy_path_when_no_adapter(monkeypatch):
    """With no adapter registered, run_edgar falls back to the legacy direct call
    (ticker popped, ``company.<method>()`` invoked with NO kwargs, no filter)."""
    import sys

    from fd_open_data_mcp.fetch import runner
    from fd_open_data_mcp.fetch.runner import run_upstream

    filings_direct = _FakeCompanyFilings(pd.DataFrame({
        "filing_date": ["2024-07-26"], "form": ["10-K"],
    }))
    fake = _FakeEdgar([filings_direct])
    monkeypatch.setitem(sys.modules, "edgar", fake)
    monkeypatch.setenv("EDGAR_IDENTITY", "test@example.com")
    runner._EDGAR_IDENTITY_SET = False
    adapters._REGISTRY.clear()
    out = run_upstream("edgar", "company_get_filings", {"ticker": "AAPL"})
    assert out is filings_direct
    assert fake._company.calls == 1              # no retry on the legacy path
    # legacy path applies NO filing_date filter
    assert filings_direct.filter_calls == []


# ---------------------------------------------------------------------------
# edinet adapter (task 2.3)
# ---------------------------------------------------------------------------


class _FakeEdinetDocument:
    """Fake edinet ``Document``: filing-metadata properties (no fetch/parse)."""

    def __init__(self, doc_id, doc_type_code, filer_name, filing_datetime, **extra):
        self.doc_id = doc_id
        self.doc_type_code = doc_type_code
        self.doc_type_name = extra.get("doc_type_name", "Securities Report")
        self.filer_edinet_code = extra.get("filer_edinet_code", "E02144")
        self.filer_name = filer_name
        self.filing_datetime = filing_datetime
        self.securities_code = extra.get("securities_code", "72030")
        self.period_start = extra.get("period_start", "2023-04-01")
        self.period_end = extra.get("period_end", "2024-03-31")
        self.doc_description = extra.get("doc_description", "")


class _FakeEdinetEntity:
    """Fake ``edinet_tools.Entity``: ``documents()`` raises/returns per a script."""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[dict] = []
        self.code = None

    def documents(self, **kwargs):
        self.calls.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _FakeEdinetTools:
    """Fake ``edinet_tools`` module: ``configure`` records, ``entity`` factory."""

    def __init__(self, script):
        self._entity = _FakeEdinetEntity(script)
        self.configure_calls: list = []

    def configure(self, api_key=None, **kwargs):
        self.configure_calls.append(api_key)

    def entity(self, code):
        self._entity.code = code
        return self._entity


def _edinet_docs():
    import datetime as _dt

    return [
        _FakeEdinetDocument("S100AAA", "120", "Toyota", _dt.datetime(2024, 7, 25, 9, 0)),
        _FakeEdinetDocument("S100BBB", "350", "Toyota", _dt.datetime(2024, 7, 26, 15, 30)),
        _FakeEdinetDocument("S100CCC", "140", "Toyota", _dt.datetime(2024, 7, 29, 12, 0)),
    ]


def test_edinet_adapter_registered():
    adapters._REGISTRY.clear()
    edinet_mod.register_all()
    assert adapters.has_adapter("edinet", "entity_documents")
    assert isinstance(adapters.adapter_for("edinet", "entity_documents"), EntityDocumentsAdapter)
    adapters._REGISTRY.clear()


def test_edinet_adapter_build_params():
    a = EntityDocumentsAdapter()
    # single date -> code + date (normalized ISO)
    params = a.build_params(None, "7203", "2024-07-26", None)
    assert params == {"code": "7203", "date": "2024-07-26"}
    # tolerates an 8-digit YYYYMMDD requested date
    params = a.build_params(None, "E02144", "20240726", None)
    assert params == {"code": "E02144", "date": "2024-07-26"}


def test_edinet_adapter_build_range_params():
    a = EntityDocumentsAdapter()
    params = a.build_range_params(None, "7203", "2024-07-26", "2024-07-28", None)
    assert params == {"code": "7203", "start": "2024-07-26", "end": "2024-07-28"}


def test_edinet_adapter_extract_value():
    docs = _edinet_docs()
    a = EntityDocumentsAdapter()
    # list[Document] coerced to a DataFrame; match on the filing_datetime axis
    assert a.extract_value(docs, "doc_id", "2024-07-26") == "S100BBB"
    assert a.extract_value(docs, "doc_type_code", "2024-07-25") == "120"
    # date-format tolerance: request YYYYMMDD against a datetime date axis
    assert a.extract_value(docs, "doc_id", "20240729") == "S100CCC"
    # missing date / missing column -> None
    assert a.extract_value(docs, "doc_id", "2024-01-01") is None
    assert a.extract_value(docs, "nope", "2024-07-26") is None


def test_edinet_adapter_extract_value_plain_dataframe():
    # _to_dataframe is a no-op for an already-DataFrame result (defensive path
    # for callers that hand extract_value a frame directly, not list[Document]).
    a = EntityDocumentsAdapter()
    df = pd.DataFrame({
        "filing_datetime": ["2024-07-25", "2024-07-26"],
        "doc_id": ["S100AAA", "S100BBB"],
    })
    assert a.extract_value(df, "doc_id", "2024-07-26") == "S100BBB"
    assert a.extract_value(df, "doc_id", "2024-07-25") == "S100AAA"


def test_edinet_adapter_extract_series():
    docs = _edinet_docs()
    a = EntityDocumentsAdapter()
    # batch form: every (filing date, value) with start <= date <= end
    out = a.extract_series(docs, "doc_id", "2024-07-26", "2024-07-28")
    assert out == {"2024-07-26": "S100BBB"}  # 07-25 before start, 07-29 after end
    out = a.extract_series(docs, "doc_id", "2024-07-25", "2024-07-29")
    assert out == {"2024-07-25": "S100AAA", "2024-07-26": "S100BBB", "2024-07-29": "S100CCC"}


def test_edinet_adapter_call_retries_then_succeeds(monkeypatch):
    import datetime as _dt
    import sys

    from fd_open_data_mcp.fetch import runner

    docs_ok = _edinet_docs()
    fake = _FakeEdinetTools([RuntimeError("boom"), RuntimeError("boom"), docs_ok])
    monkeypatch.setitem(sys.modules, "edinet_tools", fake)
    monkeypatch.setenv("EDINET_API_KEY", "test-key")
    runner._EDINET_CONFIGURED = False

    a = EntityDocumentsAdapter()
    a._RETRIES = 2
    a._RETRY_DELAY = 0  # don't actually sleep in tests
    params = a.build_params(None, "7203", "2024-07-26", None)
    params["doc_type"] = "120"

    result = a.call("entity_documents", params)
    assert result is docs_ok
    assert len(fake._entity.calls) == 3           # 2 failures + 1 success
    assert fake.configure_calls == ["test-key"]   # API key configured once
    # lookback window covers the anchor date (+ margin), doc_type passed through
    expected_days = (_dt.date.today() - _dt.date(2024, 7, 26)).days + 7
    assert fake._entity.calls[-1] == {"days": expected_days, "doc_type": "120"}
    assert fake._entity.code == "7203"


def test_edinet_adapter_call_raises_fetcherror_after_exhausting_retries(monkeypatch):
    import sys

    from fd_open_data_mcp.fetch import runner
    from fd_open_data_mcp.fetch.runner import FetchError

    fake = _FakeEdinetTools([RuntimeError("boom")] * 3)
    monkeypatch.setitem(sys.modules, "edinet_tools", fake)
    monkeypatch.setenv("EDINET_API_KEY", "test-key")
    runner._EDINET_CONFIGURED = False
    a = EntityDocumentsAdapter()
    a._RETRIES = 2
    a._RETRY_DELAY = 0

    with pytest.raises(FetchError):
        a.call("entity_documents", a.build_params(None, "7203", "2024-07-26", None))
    assert len(fake._entity.calls) == 3


def test_edinet_adapter_call_missing_code_raises(monkeypatch):
    import sys

    from fd_open_data_mcp.fetch import runner
    from fd_open_data_mcp.fetch.runner import FetchError

    fake = _FakeEdinetTools(["unused"])
    monkeypatch.setitem(sys.modules, "edinet_tools", fake)
    monkeypatch.setenv("EDINET_API_KEY", "test-key")
    runner._EDINET_CONFIGURED = False
    a = EntityDocumentsAdapter()
    with pytest.raises(FetchError):
        a.call("entity_documents", {"date": "2024-07-26"})


def test_edinet_adapter_call_raises_when_api_key_unset(monkeypatch):
    """No EDINET_API_KEY -> FetchError before any upstream call (documents path)."""
    import sys

    from fd_open_data_mcp.fetch import runner
    from fd_open_data_mcp.fetch.runner import FetchError

    fake = _FakeEdinetTools(["unused"])
    monkeypatch.setitem(sys.modules, "edinet_tools", fake)
    monkeypatch.delenv("EDINET_API_KEY", raising=False)
    runner._EDINET_CONFIGURED = False
    a = EntityDocumentsAdapter()
    with pytest.raises(FetchError):
        a.call("entity_documents", {"code": "7203", "date": "2024-07-26"})
    assert len(fake._entity.calls) == 0  # never reached the upstream call


def test_run_upstream_delegates_to_edinet_adapter_call(monkeypatch):
    """run_edinet opts into the adapter's call() (retry + lookback) when registered."""
    import sys

    from fd_open_data_mcp.fetch import runner
    from fd_open_data_mcp.fetch.runner import run_upstream

    docs_ok = _edinet_docs()
    fake = _FakeEdinetTools([RuntimeError("transient"), docs_ok])
    monkeypatch.setitem(sys.modules, "edinet_tools", fake)
    monkeypatch.setenv("EDINET_API_KEY", "test-key")
    runner._EDINET_CONFIGURED = False
    a = EntityDocumentsAdapter()
    a._RETRIES = 1
    a._RETRY_DELAY = 0
    adapters.register("edinet", "entity_documents", a)
    try:
        out = run_upstream("edinet", "entity_documents",
                           a.build_params(None, "7203", "2024-07-26", None))
        assert a.extract_value(out, "doc_id", "2024-07-26") == "S100BBB"
        assert len(fake._entity.calls) == 2      # 1 failure retried, then success
    finally:
        adapters._REGISTRY.clear()


def test_run_upstream_edinet_legacy_path_when_no_adapter(monkeypatch):
    """With no adapter registered, run_edinet falls back to the legacy direct call
    (code popped, ``entity.<method>()`` invoked with NO kwargs, no lookback)."""
    import sys

    from fd_open_data_mcp.fetch import runner
    from fd_open_data_mcp.fetch.runner import run_upstream

    docs_direct = _edinet_docs()
    fake = _FakeEdinetTools([docs_direct])
    monkeypatch.setitem(sys.modules, "edinet_tools", fake)
    monkeypatch.setenv("EDINET_API_KEY", "test-key")
    runner._EDINET_CONFIGURED = False
    adapters._REGISTRY.clear()
    out = run_upstream("edinet", "entity_documents", {"code": "7203"})
    assert out is docs_direct
    assert fake._entity.calls == [{}]            # no kwargs on the legacy path
    assert fake.configure_calls == ["test-key"]  # documents path requires the key


# ---------------------------------------------------------------------------
# dartlab adapter (task 2.4)
# ---------------------------------------------------------------------------


class _FakeDartlabCompany:
    """Fake company proxy returned by ``dartlab.Company(code)``.

    Models the dartlab company surface: ``panel``/``credit``/``analysis``
    (callable properties — faked as plain methods), ``news``/``disclosure``
    (methods). Each call pops the next scripted result (or raises if it is an
    Exception) and records its kwargs so tests can assert on the call shape.
    """

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[dict] = []
        self.code = None

    def _next(self):
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def panel(self, **kwargs):
        self.calls.append({"method": "panel", **kwargs})
        return self._next()

    def credit(self, axis=None, **kwargs):
        self.calls.append({"method": "credit", "axis": axis, **kwargs})
        return self._next()

    def analysis(self, axis=None, subaxis=None):
        self.calls.append({"method": "analysis", "axis": axis, "subaxis": subaxis})
        return self._next()

    def news(self, *, days=30):
        self.calls.append({"method": "news", "days": days})
        return self._next()

    def disclosure(self, start, end, **kwargs):
        self.calls.append(
            {"method": "disclosure", "start": start, "end": end, **kwargs}
        )
        return self._next()


class _FakeCompanyFactory:
    """``dartlab.Company`` is a callable (factory) with a ``.search`` staticmethod."""

    def __init__(self, company_proxy):
        self._proxy = company_proxy

    def __call__(self, code):
        self._proxy.code = code
        return self._proxy

    def search(self, keyword, *, limit=None):
        self._proxy.calls.append(
            {"method": "search", "keyword": keyword, "limit": limit}
        )
        return self._proxy._next()


class _FakeDartlabModule:
    """Fake ``dartlab`` module: ``Company`` factory, NO ``configure()``.

    The DART key is read directly by dartlab at call time, so the module exposes
    no ``configure`` step (unlike edinet-tools).
    """

    def __init__(self, script):
        self._company = _FakeDartlabCompany(script)
        self.Company = _FakeCompanyFactory(self._company)


def _dartlab_panel():
    """Wide accounting panel (항목 rows × period columns)."""
    return pd.DataFrame({
        "항목": ["매출액", "영업이익", "당기순이익"],
        "2024": [1000, 200, 150],
        "2025Q1": [250, 50, 38],
    })


def _dartlab_news():
    return pd.DataFrame({
        "title": ["A", "B", "C"],
        "date": ["2024-07-25", "2024-07-26", "2024-07-29"],
        "source": ["K", "M", "N"],
        "link": ["x", "y", "z"],
    })


def _dartlab_disclosure():
    return pd.DataFrame({
        "docId": ["D1", "D2", "D3"],
        "filedAt": ["2024-07-25", "2024-07-26", "2024-07-29"],
        "title": ["T1", "T2", "T3"],
        "formType": ["10-K", "8-K", "10-Q"],
    })


def _dartlab_credit():
    return {"grade": "AAA", "score": 95.0, "healthScore": 90.0, "outlook": "Stable"}


def _dartlab_search():
    return pd.DataFrame({
        "stockCode": ["005930", "000660"],
        "corpName": ["삼성전자", "SK하이닉스"],
        "market": ["KOSPI", "KOSPI"],
        "sector": ["전자", "반도체"],
    })


def test_dartlab_adapter_registered():
    adapters._REGISTRY.clear()
    dartlab_mod.register_all()
    assert adapters.has_adapter("dartlab", "company_panel")
    assert adapters.has_adapter("dartlab", "company_credit")
    assert adapters.has_adapter("dartlab", "company_analysis")
    assert adapters.has_adapter("dartlab", "company_news")
    assert adapters.has_adapter("dartlab", "company_disclosure")
    assert adapters.has_adapter("dartlab", "company_search")
    assert isinstance(adapters.adapter_for("dartlab", "company_panel"), CompanyPanelAdapter)
    adapters._REGISTRY.clear()


def test_dartlab_adapter_build_params():
    a = CompanyPanelAdapter()
    assert a.build_params(None, "005930", "2024-12-31", None) == {"code": "005930"}
    # search builds params from the identifier-as-keyword, not code
    s = CompanySearchAdapter()
    assert s.build_params(None, "삼성", None, None) == {"keyword": "삼성"}


def test_dartlab_adapter_build_range_params():
    a = CompanyDisclosureAdapter()
    params = a.build_range_params(None, "005930", "2024-07-26", "2024-07-28", None)
    assert params == {"code": "005930", "start": "2024-07-26", "end": "2024-07-28"}


def test_dartlab_adapter_extract_value_wide():
    panel = _dartlab_panel()
    a = CompanyPanelAdapter()
    # account name → row (label col '항목'); date → period column
    assert a.extract_value(panel, "매출액", "2024-12-31") == 1000
    assert a.extract_value(panel, "영업이익", "2024-12-31") == 200
    # quarter period header '2025Q1' → 2025-03-31
    assert a.extract_value(panel, "매출액", "2025-03-31") == 250
    # missing date / missing label → None
    assert a.extract_value(panel, "매출액", "2023-12-31") is None
    assert a.extract_value(panel, "없는항목", "2024-12-31") is None


def test_dartlab_adapter_extract_value_long():
    news = _dartlab_news()
    a = CompanyNewsAdapter()
    assert a.extract_value(news, "title", "2024-07-26") == "B"
    # 8-digit date tolerance
    assert a.extract_value(news, "title", "20240729") == "C"
    assert a.extract_value(news, "title", "2024-01-01") is None
    # disclosure uses aliases (doc_id → docId, filing axis = filedAt)
    disc = _dartlab_disclosure()
    d = CompanyDisclosureAdapter()
    assert d.extract_value(disc, "doc_id", "2024-07-26") == "D2"
    assert d.extract_value(disc, "form", "2024-07-29") == "10-Q"


def test_dartlab_adapter_extract_value_dict():
    credit = _dartlab_credit()
    a = CompanyCreditAdapter()
    assert a.extract_value(credit, "grade", None) == "AAA"
    assert a.extract_value(credit, "score", None) == 95.0
    assert a.extract_value(credit, "nope", None) is None
    # credit is point-in-time: extract_series returns {}
    assert a.extract_series(credit, "grade", "2024-01-01", "2024-12-31") == {}


def test_dartlab_adapter_extract_series_wide():
    panel = _dartlab_panel()
    a = CompanyPanelAdapter()
    out = a.extract_series(panel, "매출액", "2024-01-01", "2025-12-31")
    assert out == {"2024-12-31": 1000, "2025-03-31": 250}
    # narrow window excludes both periods
    out = a.extract_series(panel, "매출액", "2025-04-01", "2025-12-31")
    assert out == {}


def test_dartlab_adapter_call_retries_then_succeeds(monkeypatch):
    import sys

    from fd_open_data_mcp.fetch import runner

    panel_ok = _dartlab_panel()
    fake = _FakeDartlabModule([RuntimeError("boom"), RuntimeError("boom"), panel_ok])
    monkeypatch.setitem(sys.modules, "dartlab", fake)
    monkeypatch.setenv("DART_API_KEY", "test-key")
    runner._DART_KEY_CHECKED = False

    a = CompanyPanelAdapter()
    a._RETRIES = 2
    a._RETRY_DELAY = 0
    params = a.build_params(None, "005930", "2024-12-31", None)

    result = a.call("company_panel", params)
    assert result is panel_ok
    assert len(fake._company.calls) == 3            # 2 failures + 1 success
    assert fake._company.calls[-1] == {"method": "panel"}  # no kwargs (key/freq absent)
    assert fake._company.code == "005930"


def test_dartlab_adapter_call_raises_fetcherror_after_exhausting_retries(monkeypatch):
    import sys

    from fd_open_data_mcp.fetch import runner
    from fd_open_data_mcp.fetch.runner import FetchError

    fake = _FakeDartlabModule([RuntimeError("boom")] * 3)
    monkeypatch.setitem(sys.modules, "dartlab", fake)
    monkeypatch.setenv("DART_API_KEY", "test-key")
    runner._DART_KEY_CHECKED = False
    a = CompanyPanelAdapter()
    a._RETRIES = 2
    a._RETRY_DELAY = 0

    with pytest.raises(FetchError):
        a.call("company_panel", a.build_params(None, "005930", "2024-12-31", None))
    assert len(fake._company.calls) == 3


def test_dartlab_adapter_call_missing_code_raises(monkeypatch):
    import sys

    from fd_open_data_mcp.fetch import runner
    from fd_open_data_mcp.fetch.runner import FetchError

    fake = _FakeDartlabModule(["unused"])
    monkeypatch.setitem(sys.modules, "dartlab", fake)
    monkeypatch.setenv("DART_API_KEY", "test-key")
    runner._DART_KEY_CHECKED = False
    a = CompanyPanelAdapter()
    with pytest.raises(FetchError):
        a.call("company_panel", {"key": "is"})
    assert len(fake._company.calls) == 0  # code guard fired first


def test_dartlab_adapter_call_raises_when_api_key_unset(monkeypatch):
    """No DART_API_KEY → FetchError before any upstream call (credentialed path)."""
    import sys

    from fd_open_data_mcp.fetch import runner
    from fd_open_data_mcp.fetch.runner import FetchError

    fake = _FakeDartlabModule(["unused"])
    monkeypatch.setitem(sys.modules, "dartlab", fake)
    monkeypatch.delenv("DART_API_KEY", raising=False)
    monkeypatch.delenv("DART_API_KEYS", raising=False)
    runner._DART_KEY_CHECKED = False
    a = CompanyPanelAdapter()
    with pytest.raises(FetchError):
        a.call("company_panel", {"code": "005930", "date": "2024-12-31"})
    assert len(fake._company.calls) == 0  # key guard fired first


def test_dartlab_adapter_keyless_paths_skip_key_guard(monkeypatch):
    """search + news are keyless public endpoints: no DART_API_KEY needed."""
    import sys

    from fd_open_data_mcp.fetch import runner

    # No DART key set at all.
    monkeypatch.delenv("DART_API_KEY", raising=False)
    monkeypatch.delenv("DART_API_KEYS", raising=False)
    runner._DART_KEY_CHECKED = False

    # company_search: keyless + codeless
    search_ok = _dartlab_search()
    fake_s = _FakeDartlabModule([search_ok])
    monkeypatch.setitem(sys.modules, "dartlab", fake_s)
    s = CompanySearchAdapter()
    s._RETRIES = 0
    s._RETRY_DELAY = 0
    out = s.call("company_search", {"keyword": "삼성"})
    assert out is search_ok
    assert s.extract_value(out, "code", None) == "005930"  # alias code→stockCode

    # company_news: keyless but needs a code
    news_ok = _dartlab_news()
    fake_n = _FakeDartlabModule([news_ok])
    monkeypatch.setitem(sys.modules, "dartlab", fake_n)
    runner._DART_KEY_CHECKED = False  # reset between adapters
    n = CompanyNewsAdapter()
    n._RETRIES = 0
    n._RETRY_DELAY = 0
    out = n.call("company_news", {"code": "005930"})
    assert out is news_ok
    assert n.extract_value(out, "title", "2024-07-26") == "B"


def test_run_upstream_delegates_to_dartlab_adapter_call(monkeypatch):
    """run_dartlab opts into the adapter's call() (retry + coercion) when registered."""
    import sys

    from fd_open_data_mcp.fetch import runner
    from fd_open_data_mcp.fetch.runner import run_upstream

    panel_ok = _dartlab_panel()
    fake = _FakeDartlabModule([RuntimeError("transient"), panel_ok])
    monkeypatch.setitem(sys.modules, "dartlab", fake)
    monkeypatch.setenv("DART_API_KEY", "test-key")
    runner._DART_KEY_CHECKED = False
    a = CompanyPanelAdapter()
    a._RETRIES = 1
    a._RETRY_DELAY = 0
    adapters.register("dartlab", "company_panel", a)
    try:
        out = run_upstream("dartlab", "company_panel",
                           a.build_params(None, "005930", "2024-12-31", None))
        assert a.extract_value(out, "매출액", "2024-12-31") == 1000
        assert len(fake._company.calls) == 2   # 1 failure retried, then success
    finally:
        adapters._REGISTRY.clear()


def test_run_upstream_dartlab_legacy_path_when_no_adapter(monkeypatch):
    """With no adapter registered, run_dartlab falls back to the legacy direct
    call (code popped, ``company.<method>()`` invoked with NO kwargs)."""
    import sys

    from fd_open_data_mcp.fetch import runner
    from fd_open_data_mcp.fetch.runner import run_upstream

    panel_direct = _dartlab_panel()
    fake = _FakeDartlabModule([panel_direct])
    monkeypatch.setitem(sys.modules, "dartlab", fake)
    monkeypatch.setenv("DART_API_KEY", "test-key")
    runner._DART_KEY_CHECKED = False
    adapters._REGISTRY.clear()
    out = run_upstream("dartlab", "company_panel", {"code": "005930"})
    assert out is panel_direct
    assert fake._company.calls == [{"method": "panel"}]  # no kwargs on the legacy path


# ---------------------------------------------------------------------------
# cnstats adapter (task 2.6) - 8 curated NBS macro indicators backed by akshare.
# Keyless (no env var); macro functions take no args (build_params -> {}); the
# 日期 date axis + Chinese-named columns drive extract_value/extract_series.
# ---------------------------------------------------------------------------

class _FakeCnstatsAkshare:
    """A fake `akshare` module exposing the mapped macro functions as scripted
    callables (no args). ``getattr(fake, <macro_name>)`` returns a callable that
    pops the next scripted item on each call: raises if it's an Exception, else
    returns it. Dunder attribute access raises ``AttributeError`` so Python's
    normal attribute protocol keeps working during the test."""

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[str] = []  # which macro function was invoked

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        def _fn():
            self.calls.append(name)
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        return _fn


def _cpi_frame():
    """A small CPI macro frame: 日期 column + 全国同比, with one NaN cell."""
    return pd.DataFrame({
        "日期": ["2024-01-01", "2024-02-01", "2024-03-01"],
        "全国同比": [2.1, 2.5, float("nan")],
    })


def _cpi_frame_indexed():
    """A CPI frame whose date axis is the index (name=日期), not a column."""
    df = pd.DataFrame({"全国同比": [2.1, 2.5]}, index=["2024-01-01", "2024-02-01"])
    df.index.name = "日期"
    return df


def _clear_cnstats():
    """Remove only the cnstats adapter keys (leave other sources registered)."""
    for cmd in list(CNSTATS_MAPPING):
        adapters._REGISTRY.pop(("cnstats", cmd), None)


def test_cnstats_mapping_has_eight_commands():
    assert len(CNSTATS_MAPPING) == 8
    # the adapter commands match the seed REGISTRY keys
    from fd_open_data_mcp.catalog.seeds.cnstats import REGISTRY as SEED

    assert set(CNSTATS_MAPPING) == set(SEED)


def test_cnstats_build_params_returns_empty():
    a = _CnstatsBase()
    assert a.build_params(None, "", "", None) == {}
    assert a.build_range_params(None, "", "", "", None) == {}


def test_cnstats_register_all_registers_eight_shared_instances():
    adapters._REGISTRY.clear()
    try:
        cnstats_mod.register_all()
        keys = [k for k in adapters.registered() if k[0] == "cnstats"]
        assert len(keys) == 8
        # all 8 commands share a single _CnstatsBase instance
        ids = {id(adapters.adapter_for("cnstats", c)) for c in CNSTATS_MAPPING}
        assert len(ids) == 1
        assert isinstance(adapters.adapter_for("cnstats", "cpi"), _CnstatsBase)
    finally:
        cnstats_mod.register_all()


def test_cnstats_extract_value_picks_row_by_date():
    a = _CnstatsBase()
    df = _cpi_frame()
    assert a.extract_value(df, "全国同比", "2024-02-01") == 2.5
    assert a.extract_value(df, "全国同比", "2024-01-01") == 2.1


def test_cnstats_extract_value_skips_nan_cell():
    a = _CnstatsBase()
    df = _cpi_frame()
    assert a.extract_value(df, "全国同比", "2024-03-01") is None


def test_cnstats_extract_value_none_when_date_missing():
    a = _CnstatsBase()
    df = _cpi_frame()
    assert a.extract_value(df, "全国同比", "2024-12-01") is None


def test_cnstats_extract_value_none_when_column_missing():
    a = _CnstatsBase()
    df = _cpi_frame()
    assert a.extract_value(df, "不存在", "2024-01-01") is None


def test_cnstats_extract_value_none_when_not_dataframe():
    a = _CnstatsBase()
    assert a.extract_value(None, "全国同比", "2024-01-01") is None
    assert a.extract_value([], "全国同比", "2024-01-01") is None
    assert a.extract_value(pd.DataFrame(), "全国同比", "2024-01-01") is None


def test_cnstats_extract_value_uses_index_when_no_date_column():
    a = _CnstatsBase()
    df = _cpi_frame_indexed()
    assert a.extract_value(df, "全国同比", "2024-02-01") == 2.5


def test_cnstats_extract_series_range_filters_and_skips_nan():
    a = _CnstatsBase()
    df = _cpi_frame()
    out = a.extract_series(df, "全国同比", "2024-01-01", "2024-02-01")
    assert out == {"2024-01-01": 2.1, "2024-02-01": 2.5}


def test_cnstats_extract_series_excludes_out_of_range_and_nan():
    a = _CnstatsBase()
    df = _cpi_frame()
    # full range: the NaN March cell is skipped, only the two real values remain
    out = a.extract_series(df, "全国同比", "2024-01-01", "2024-12-31")
    assert out == {"2024-01-01": 2.1, "2024-02-01": 2.5}


def test_cnstats_extract_series_empty_when_no_dataframe():
    a = _CnstatsBase()
    assert a.extract_series(None, "全国同比", "2024-01-01", "2024-12-31") == {}
    assert a.extract_series(pd.DataFrame(), "全国同比", "2024-01-01", "2024-12-31") == {}


def test_cnstats_call_retries_then_succeeds(monkeypatch):
    import sys

    fake = _FakeCnstatsAkshare([RuntimeError("boom"), RuntimeError("boom"), "OK"])
    monkeypatch.setitem(sys.modules, "akshare", fake)
    a = _CnstatsBase()
    a._RETRIES = 2
    a._RETRY_DELAY = 0  # don't actually sleep in tests

    result = a.call("cpi", {})
    assert result == "OK"
    assert len(fake.calls) == 3                       # 2 failures + 1 success
    assert set(fake.calls) == {"macro_china_cpi_yearly"}


def test_cnstats_call_raises_fetcherror_after_exhausting_retries(monkeypatch):
    import sys

    from fd_open_data_mcp.fetch.runner import FetchError

    fake = _FakeCnstatsAkshare([RuntimeError("boom")] * 3)
    monkeypatch.setitem(sys.modules, "akshare", fake)
    a = _CnstatsBase()
    a._RETRIES = 2
    a._RETRY_DELAY = 0

    with pytest.raises(FetchError):
        a.call("cpi", {})
    assert len(fake.calls) == 3


def test_cnstats_call_unknown_command_raises_fetcherror():
    from fd_open_data_mcp.fetch.runner import FetchError

    a = _CnstatsBase()
    with pytest.raises(FetchError):
        a.call("not_a_real_command", {})


def test_run_upstream_cnstats_delegates_to_adapter_call(monkeypatch):
    """run_cnstats opts into the adapter's call() (retry) when one is registered."""
    import sys

    from fd_open_data_mcp.fetch.runner import run_upstream

    fake = _FakeCnstatsAkshare([RuntimeError("transient"), "OK-MACRO"])
    monkeypatch.setitem(sys.modules, "akshare", fake)
    a = _CnstatsBase()
    a._RETRIES = 1
    a._RETRY_DELAY = 0
    adapters.register("cnstats", "cpi", a)
    try:
        out = run_upstream("cnstats", "cpi", {})
        assert out == "OK-MACRO"
        assert len(fake.calls) == 2                    # 1 failure retried, then success
        assert fake.calls[0] == "macro_china_cpi_yearly"
    finally:
        _clear_cnstats()
        cnstats_mod.register_all()


def test_run_upstream_cnstats_legacy_path_when_no_adapter(monkeypatch):
    """With no cnstats adapter registered, run_cnstats falls back to a direct
    getattr(ak, MAPPING[command])() call (no retry)."""
    import sys

    from fd_open_data_mcp.fetch.runner import run_upstream

    fake = _FakeCnstatsAkshare(["DIRECT-OK"])
    monkeypatch.setitem(sys.modules, "akshare", fake)
    _clear_cnstats()
    try:
        out = run_upstream("cnstats", "cpi", {})
        assert out == "DIRECT-OK"
        assert len(fake.calls) == 1                    # no retry on the legacy path
        assert fake.calls[0] == "macro_china_cpi_yearly"
    finally:
        cnstats_mod.register_all()
