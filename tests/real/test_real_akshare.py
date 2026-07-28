"""Real-fetch: akshare stock_zh_a_hist -> 收盘."""
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.network


def test_run_akshare_returns_close(is_finite):
    pytest.importorskip("akshare")
    from fd_open_data_mcp.fetch.dispatch import _extract_value
    from fd_open_data_mcp.fetch.runner import run_akshare

    end = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y%m%d")
    start = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y%m%d")
    df = run_akshare("stock_zh_a_hist", {
        "symbol": "600519", "period": "daily", "start_date": start, "end_date": end,
    })
    assert df is not None and not df.empty and "收盘" in df.columns
    # pick the first row's date and extract its close
    date = str(df.iloc[0]["日期"]) if "日期" in df.columns else str(df.index[0])
    val = _extract_value(df, "收盘", date)
    assert val is not None and is_finite(val), f"val={val}"
