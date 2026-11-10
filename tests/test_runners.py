"""Unit tests for new runners."""
import pytest
from fd_open_data_mcp.adapters.nbs_gdp import run_nbs_gdp, _fetch_gdp_quarterly
from fd_open_data_mcp.errors import FetchError


def test_nbs_gdp_runner():
    """Test NBS GDP runner basic functionality.

    Needs the ``[data]`` extra (akshare) AND live ``data.stats.gov.cn`` — this is
    an integration check in a unit suite. Verified working in the deployed crawl
    image on 2026-11-10 (7 rows, 2020Q1 = 209671.1); a venv without the extra
    reports ``akshare not installed`` and used to fail the suite rather than skip.
    """
    pytest.importorskip("akshare", reason="NBS GDP runner needs the [data] extra")
    result = run_nbs_gdp('get_gdp_quarterly', {'start_year': 2020})
    assert 'period' in result.columns
    assert 'value' in result.columns
    assert len(result) > 0


def test_fetch_error_handling():
    """Test that fetch errors are properly raised."""
    with pytest.raises(FetchError):
        run_nbs_gdp('invalid_command', {})


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
