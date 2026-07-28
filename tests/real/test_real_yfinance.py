"""Real-fetch: yfinance ticker_history -> Close."""
import pytest

pytestmark = pytest.mark.network


def test_run_yfinance_returns_close(is_finite):
    pytest.importorskip("yfinance")
    import pandas as pd

    from fd_open_data_mcp.fetch.runner import run_yfinance

    df = run_yfinance("ticker_history", {"symbol": "AAPL", "period": "1mo", "interval": "1d"})
    assert isinstance(df, pd.DataFrame) and "Close" in df.columns and not df.empty
    row = df.dropna(subset=["Close"]).iloc[0]
    assert is_finite(row["Close"]), f"close={row['Close']}"
