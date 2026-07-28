"""Real-fetch: wbgapi get_indicator_data -> GDP (current US$)."""
import pytest

pytestmark = pytest.mark.network


def test_run_wbgapi_returns_gdp(is_finite):
    pytest.importorskip("wbgapi")
    import pandas as pd

    from fd_open_data_mcp.fetch.runner import run_wbgapi

    df = run_wbgapi("get_indicator_data", {
        "indicator": "NY.GDP.MKTP.CD", "economy": "USA", "date": "2022",
    })
    assert isinstance(df, pd.DataFrame) and not df.empty
    val = df.iloc[0, 0]
    assert val is not None and is_finite(val), f"gdp={val}"
