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
from fd_open_data_mcp.fetch.dispatch import _build_params, _extract_value
from fd_open_data_mcp.models import Function, FunctionColumn, Source


class _DummyAdapter:
    """An adapter that maps by position (not name-guessing) to prove delegation."""

    def build_params(self, fn, identifier, date, binding):
        return {"symbol": identifier, "the_date": date, "indicator": binding.column.name}

    def extract_value(self, result, column_name, date):
        # deliberate non-legacy extraction: read by position, not date-index
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
