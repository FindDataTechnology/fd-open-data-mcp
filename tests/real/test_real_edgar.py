"""Real-fetch: edgar company_get_financials (needs EDGAR_IDENTITY)."""
import os

import pytest

pytestmark = pytest.mark.network


def test_run_edgar_returns_financials():
    pytest.importorskip("edgar")  # edgartools imports as `edgar`
    if not os.environ.get("EDGAR_IDENTITY"):
        pytest.skip("EDGAR_IDENTITY not set")
    from fd_open_data_mcp.fetch.runner import run_edgar

    result = run_edgar("company_get_financials", {"ticker": "AAPL"})
    # edgar returns a structured Financials object (design D7); assert the fetch path worked.
    assert result is not None
