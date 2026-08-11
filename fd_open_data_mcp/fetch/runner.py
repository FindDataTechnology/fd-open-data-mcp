"""Provider runner: invoke an upstream package callable with resolved params.

Best-effort in v1: handles akshare (module callables) and yfinance (Ticker
methods + top-level). Requires the `data` extra. ``returned_columns`` extracts
(name, type) from a result for sample-confirmation (spec semantic-layer).
"""
from __future__ import annotations

from typing import Any


class FetchError(Exception):
    """Raised when an upstream call cannot be made or fails."""


def run_akshare(command: str, params: dict) -> Any:
    # If a per-function adapter with a ``call`` method is registered, delegate to
    # it (the adapter applies the native akshare ``timeout`` kwarg + a simple
    # retry for transient failures - task 2.3). Otherwise fall back to a direct
    # call, so un-adaptered functions keep working (no regression).
    from fd_open_data_mcp.adapters import adapter_for

    adapter = adapter_for("akshare", command)
    call = getattr(adapter, "call", None)
    if call is not None:
        return call(command, params)

    import akshare as ak  # lazy; requires the `data` extra

    fn = getattr(ak, command, None)
    if fn is None or not callable(fn):
        raise FetchError(f"akshare has no callable {command}")
    try:
        return fn(**params)
    except Exception as e:  # noqa: BLE001 - upstream errors surface as FetchError
        raise FetchError(f"akshare {command} raised: {e}") from e


def run_yfinance(command: str, params: dict) -> Any:
    import yfinance as yf  # lazy; requires the `data` extra

    if command.startswith("ticker_"):
        method = command[len("ticker_"):]
        symbol = params.pop("symbol", None)
        if symbol is None:
            raise FetchError(f"ticker_* command {command} needs a symbol")
        ticker = yf.Ticker(symbol)
        attr = getattr(ticker, method, None)
        if attr is None:
            raise FetchError(f"yfinance.Ticker has no method {method}")
        return attr() if callable(attr) else attr
    fn = getattr(yf, command, None)
    if fn is None or not callable(fn):
        raise FetchError(f"yfinance has no callable {command}")
    try:
        return fn(**params)
    except Exception as e:  # noqa: BLE001
        raise FetchError(f"yfinance {command} raised: {e}") from e


_EDGAR_IDENTITY_SET = False


def _ensure_edgar_identity() -> None:
    """Call ``edgar.set_identity(EDGAR_IDENTITY)`` once. Raise FetchError if unset."""
    global _EDGAR_IDENTITY_SET
    if _EDGAR_IDENTITY_SET:
        return
    import os

    identity = os.environ.get("EDGAR_IDENTITY")
    if not identity:
        raise FetchError(
            "EDGAR_IDENTITY env var is not set; the SEC requires a User-Agent identity"
        )
    import edgar
    edgar.set_identity(identity)
    _EDGAR_IDENTITY_SET = True


def run_edgar(command: str, params: dict) -> Any:
    import edgar  # lazy; requires the `data` extra (edgartools dist)
    _ensure_edgar_identity()
    if command.startswith("company_"):
        method = command[len("company_"):]
        ticker = params.get("ticker")
        if ticker is None:
            raise FetchError(f"company_* command {command} needs a ticker")
        company = edgar.Company(ticker)
        attr = getattr(company, method, None)
        if attr is None:
            raise FetchError(f"edgar.Company has no method {method}")
        try:
            return attr() if callable(attr) else attr
        except Exception as e:  # noqa: BLE001
            raise FetchError(f"edgar Company.{method} raised: {e}") from e
    fn = getattr(edgar, command, None)
    if fn is None or not callable(fn):
        raise FetchError(f"edgar has no callable {command}")
    try:
        return fn(**params)
    except Exception as e:  # noqa: BLE001
        raise FetchError(f"edgar {command} raised: {e}") from e


def run_wbgapi(command: str, params: dict) -> Any:
    import pandas as pd
    import wbgapi as wb  # lazy; requires the `data` extra

    if command == "get_indicator_data":
        indicator = params.get("indicator")
        economy = params.get("economy")
        date = params.get("date") or params.get("time") or params.get("start")
        if not indicator or not economy:
            raise FetchError("get_indicator_data needs indicator + economy")
        try:
            year = int(str(date)[:4]) if date else None
            time_range = range(year, year + 1) if year else None
            df = wb.data.DataFrame(indicator, economy, time=time_range, labels=False)
        except Exception as e:  # noqa: BLE001
            raise FetchError(f"wbgapi get_indicator_data raised: {e}") from e
        if df is None or getattr(df, "empty", True):
            raise FetchError("wbgapi returned no data")
        val = df.iloc[0, 0]
        date_key = str(year) if year else str(df.columns[0])
        # Reshape so _extract_value (column=indicator, index=date) finds it.
        return pd.DataFrame({indicator: [val]}, index=[date_key])
    try:
        if command == "list_indicators":
            q = params.get("q")
            rows = list(wb.series.info(q=q)) if q else list(wb.series.info())
            return pd.DataFrame(rows)
        if command == "list_economies":
            return wb.economy.DataFrame()
        if command == "get_series_metadata":
            return pd.DataFrame(list(wb.series.info(params.get("indicator"))))
    except Exception as e:  # noqa: BLE001
        raise FetchError(f"wbgapi {command} raised: {e}") from e
    fn = getattr(wb, command, None)
    if fn is None or not callable(fn):
        raise FetchError(f"wbgapi has no callable {command}")
    try:
        return fn(**params)
    except Exception as e:  # noqa: BLE001
        raise FetchError(f"wbgapi {command} raised: {e}") from e


def run_upstream(source: str, command: str, params: dict) -> Any:
    if source == "cn-report":
        from fd_open_data_mcp.adapters.cnreport import run_cnreport
        return run_cnreport(command, params)
    if source == "akshare":
        return run_akshare(command, params)
    if source == "yfinance":
        return run_yfinance(command, params)
    if source == "edgar":
        return run_edgar(command, params)
    if source == "wbgapi":
        return run_wbgapi(command, params)
    if source == "nbs-gdp":
        from fd_open_data_mcp.adapters.nbs_gdp import run_nbs_gdp
        return run_nbs_gdp(command, params)
    if source == "cisa-industry":
        from fd_open_data_mcp.adapters.cisa_industry import run_cisa_industry
        return run_cisa_industry(command, params)
    if source == "amac-fund":
        from fd_open_data_mcp.adapters.amac_fund import run_amac_fund
        return run_amac_fund(command, params)
    if source == "shfe-metal-futures":
        from fd_open_data_mcp.adapters.shfe_futures import run_shfe_futures
        return run_shfe_futures(command, params)
    if source == "agriculture":
        from fd_open_data_mcp.adapters.dce_agricultural import run_dce_agricultural
        return run_dce_agricultural(command, params)
    if source == "cme-agricultural-futures":
        from fd_open_data_mcp.adapters.cme_agricultural import run_cme_agricultural
        return run_cme_agricultural(command, params)
    if source == "chemicals":
        from fd_open_data_mcp.adapters.chemicals import run_chemicals
        return run_chemicals(command, params)
    if source == "electronics":
        from fd_open_data_mcp.adapters.electronics import run_electronics
        return run_electronics(command, params)
    if source == "nonferrous":
        from fd_open_data_mcp.adapters.nonferrous import run_nonferrous
        return run_nonferrous(command, params)
    if source == "flowers-kifc":
        from fd_open_data_mcp.adapters.flowers_kifc import run_flowers_kifc
        return run_flowers_kifc(command, params)
    if source == "fin_platforms":
        from fd_open_data_mcp.adapters.fin_platforms import run_fin_platforms
        return run_fin_platforms(command, params)
    if source == "sac-securities":
        from fd_open_data_mcp.adapters.sac_securities import run_sac_securities
        return run_sac_securities(command, params)
    if source == "polygon":
        # run_polygon lives in the external fd-polygon datasource package (the
        # manifest's fetch.module). Lazy-imported so fd-open-data-mcp does not
        # depend on polygon-api-client unless a polygon fetch is actually made.
        from fd_polygon.provider import run_polygon
        return run_polygon(command, params)
    if source == "datacommons":
        # run_dc lives in the external fd-datacommons datasource package (the
        # manifest's fetch.module). Lazy-imported so fd-open-data-mcp does not
        # depend on requests unless a datacommons fetch is actually made.
        from fd_datacommons.provider import run_dc
        return run_dc(command, params)
    raise FetchError(f"no runner for source {source}")


def returned_columns(result: Any) -> list[tuple[str, str]]:
    """Best-effort (name, type) extraction for sample-confirmation."""
    try:
        import pandas as pd
    except ImportError:
        return []
    if isinstance(result, pd.DataFrame):
        return [(str(c), str(result[c].dtype)) for c in result.columns]
    return []
