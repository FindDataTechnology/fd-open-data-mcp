"""Provider runner: invoke an upstream package callable with resolved params.

Best-effort in v1: handles akshare (module callables) and yfinance (Ticker
methods + top-level). Requires the `data` extra. ``returned_columns`` extracts
(name, type) from a result for sample-confirmation (spec semantic-layer).
"""
from __future__ import annotations

import contextlib
import os
from typing import Any, Optional


def _extract_http_attrs(exc: BaseException) -> tuple[Optional[int], Optional[str]]:
    """Best-effort extract ``(status_code, response_text)`` from an upstream
    exception.

    Handles ``requests.HTTPError`` and ``httpx.HTTPStatusError`` (both expose a
    ``.response`` with ``.status_code``/``.text``). Connection-level errors
    (socket, ``RemoteDisconnected``, timeout, DNS) carry no HTTP response to
    inspect, so they return ``(None, None)`` — ``ban_rules.classify`` then falls
    back to the default (transient) and the error-message substring rules.
    """
    resp = getattr(exc, "response", None)
    if resp is None:
        return None, None
    status = getattr(resp, "status_code", None)
    text = getattr(resp, "text", None)
    status_code = int(status) if status is not None else None
    return status_code, text


class FetchError(Exception):
    """Raised when an upstream call cannot be made or fails.

    Carries optional ``status_code``/``response_text`` so ``ban_rules.classify``
    can match status-based and body-based rules (HTTP 403/429/captcha). Both are
    ``None`` for connection-level errors (no HTTP response to inspect).
    """

    def __init__(self, message: str, status_code: Optional[int] = None,
                 response_text: Optional[str] = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text


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
        status, text = _extract_http_attrs(e)
        raise FetchError(f"akshare {command} raised: {e}", status, text) from e


@contextlib.contextmanager
def _yf_proxy_configured():
    """Point yfinance at this attempt's proxy for the duration of the call.

    yfinance 1.x talks over ``curl_cffi``, which our requests/httpx patches do
    not cover, so without this it egresses direct. Every direct egress in the
    fleet is blocked (measured 2026-11-08: 429 with a 23-byte `Edge: Too Many
    Requests` page from tencent, 403 with a 3,369-byte page from the cheap
    nodes), while the same endpoint returns 200 through the proxy pool with
    browser impersonation. Handing ``yf.Ticker(session=…)`` a proxied curl_cffi
    session is NOT enough - yfinance opens its own cookie/crumb connections
    (``fc.yahoo.com``) that still fail - so the proxy has to go through its
    global config.

    ``FD_YF_PROXY`` wins over the pipeline's per-attempt proxy: direct callers
    (probe job, backfill scripts) never pass through ``instrumented_fetch`` and
    have no attempt to inherit from.
    """
    try:
        import yfinance as yf  # lazy; requires the `data` extra
    except ImportError:
        yield
        return

    url = os.environ.get("FD_YF_PROXY") or None
    if not url:
        try:
            from fd_open_data_mcp.proxy import injection
            url = injection.current_proxy_url()
        except Exception:  # noqa: BLE001 - proxy plumbing must never break a fetch
            url = None
    if not url:
        yield               # no proxy selected: keep today's direct behaviour
        return

    prev_proxy = getattr(getattr(getattr(yf, "config", None), "network", None), "proxy", None)
    prev_env = {k: os.environ.get(k) for k in ("HTTP_PROXY", "HTTPS_PROXY")}
    configured = False
    try:
        try:
            set_cfg = getattr(yf, "set_config", None)
            if set_cfg is not None:
                set_cfg(proxy=url)
                configured = True
        except Exception:  # noqa: BLE001 - older/newer yfinance: fall through to env
            configured = False
        # curl_cffi reads these for its own sessions (cookie/crumb too).
        os.environ["HTTP_PROXY"] = os.environ["HTTPS_PROXY"] = url
        yield
    finally:
        for k, v in prev_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if configured:
            # Restore even when the previous value was None (direct egress): a
            # proxy must never leak into the next fetch of a long-lived process.
            with contextlib.suppress(Exception):
                yf.set_config(proxy=prev_proxy)


def run_yfinance(command: str, params: dict) -> Any:
    import yfinance as yf  # lazy; requires the `data` extra

    with _yf_proxy_configured():
        return _run_yfinance_inner(yf, command, params)


def _run_yfinance_inner(yf, command: str, params: dict) -> Any:
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
        status, text = _extract_http_attrs(e)
        raise FetchError(f"yfinance {command} raised: {e}", status, text) from e


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
            status, text = _extract_http_attrs(e)
            raise FetchError(f"edgar Company.{method} raised: {e}", status, text) from e
    fn = getattr(edgar, command, None)
    if fn is None or not callable(fn):
        raise FetchError(f"edgar has no callable {command}")
    try:
        return fn(**params)
    except Exception as e:  # noqa: BLE001
        status, text = _extract_http_attrs(e)
        raise FetchError(f"edgar {command} raised: {e}", status, text) from e


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
            status, text = _extract_http_attrs(e)
            raise FetchError(f"wbgapi get_indicator_data raised: {e}", status, text) from e
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
        status, text = _extract_http_attrs(e)
        raise FetchError(f"wbgapi {command} raised: {e}", status, text) from e
    fn = getattr(wb, command, None)
    if fn is None or not callable(fn):
        raise FetchError(f"wbgapi has no callable {command}")
    try:
        return fn(**params)
    except Exception as e:  # noqa: BLE001
        status, text = _extract_http_attrs(e)
        raise FetchError(f"wbgapi {command} raised: {e}", status, text) from e


def _run_nbs_gdp(command: str, params: dict) -> Any:
    from fd_open_data_mcp.adapters.nbs_gdp import run_nbs_gdp
    return run_nbs_gdp(command, params)


def _run_polygon(command: str, params: dict) -> Any:
    # run_polygon lives in the external fd-polygon datasource package (the
    # manifest's fetch.module). Lazy-imported so fd-open-data-mcp does not
    # depend on polygon-api-client unless a polygon fetch is actually made.
    from fd_polygon.provider import run_polygon
    return run_polygon(command, params)


def _run_datacommons(command: str, params: dict) -> Any:
    # run_dc lives in the external fd-datacommons datasource package. Lazy
    # so fd-open-data-mcp does not depend on requests unless it is used.
    from fd_datacommons.provider import run_dc
    return run_dc(command, params)


# source -> runner. The single source of truth for "can this source be called
# at all": ``fetch/capability.py`` reads this dict to answer the static
# resolvability question, so the two can never disagree about which sources
# exist. Provider runners are thin wrappers that import lazily.
RUNNERS: dict[str, Any] = {
    "akshare": run_akshare,
    "yfinance": run_yfinance,
    "edgar": run_edgar,
    "wbgapi": run_wbgapi,
    "nbs-gdp": _run_nbs_gdp,
    "polygon": _run_polygon,
    "datacommons": _run_datacommons,
}


def run_upstream(source: str, command: str, params: dict) -> Any:
    runner = RUNNERS.get(source)
    if runner is None:
        raise FetchError(f"no runner for source {source}")
    return runner(command, params)


def returned_columns(result: Any) -> list[tuple[str, str]]:
    """Best-effort (name, type) extraction for sample-confirmation."""
    try:
        import pandas as pd
    except ImportError:
        return []
    if isinstance(result, pd.DataFrame):
        return [(str(c), str(result[c].dtype)) for c in result.columns]
    return []
