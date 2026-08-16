"""yfinance per-function adapters.

yfinance exposes data through the ``Ticker`` object (design.md D6); commands
are flattened to ``ticker_<method>`` by ``catalog/upstream.py:introspect_yfinance``
(the primary dated command is ``ticker_history`` <- ``yf.Ticker(symbol).history()``).
``Ticker.history`` returns a DataFrame **indexed by date** with columns
``Open``/``High``/``Low``/``Close``/``Volume``/``Adj Close``/``Dividends``/
``Stock Splits``, so the date axis is the index (``_DATE_COL = None``), mirroring
the akshare ``stock_zh_a_daily`` adapter.

Quirks handled here (learned from yfinance, NOT from a scraw project — yfinance
had no scraw crawler):
  * ``Ticker.history(end=...)`` is EXCLUSIVE on the end date: a single-day read
    with ``start == end`` returns no rows. ``build_params`` advances ``end`` by
    one day so the requested date is inclusive.
  * The legacy ``run_yfinance`` path called ``ticker.<method>()`` with NO kwargs
    (fetching the default ~1mo history). The adapter's ``call()`` instead passes
    the date-range kwargs (``start``/``end``) through to ``.history()``, and
    wraps the call in a simple retry for transient failures (mirroring
    ``_AkshareBase.call``).
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Optional

from fd_open_data_mcp.adapters import register

logger = logging.getLogger(__name__)

# --- tunables for the optional ``call`` retry (mirror akshare) -----------------
DEFAULT_RETRIES: int = 2          # extra attempts after the first failure
DEFAULT_RETRY_DELAY: float = 1.0  # seconds between retries


# --- date helpers (yfinance returns pandas Timestamps / 'YYYY-MM-DD' strings) ---

def _normalize_date(value: Any) -> str:
    """Coerce a date cell/index to a canonical 'YYYY-MM-DD' string for matching.

    pandas ``Timestamp`` subclasses ``datetime``, so the datetime branch covers
    both python datetimes and yfinance's ``DatetimeIndex`` values. Bare strings
    are truncated to the 10-char ISO date so a full Timestamp repr also matches.
    """
    if value is None:
        return ""
    if isinstance(value, datetime):  # also covers pandas.Timestamp
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    s = str(value).strip()
    # tolerate 8-digit YYYYMMDD strings by inserting dashes (so a caller that
    # passes "20240726" still matches a 'YYYY-MM-DD' date index)
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s[:10] if len(s) >= 10 else s


def _next_day(date_str: str) -> str:
    """Return the day AFTER ``date_str`` ('YYYY-MM-DD') as 'YYYY-MM-DD'.

    yfinance ``history(end=...)`` is exclusive, so the requested date must be
    strictly less than ``end`` to be included in the result. Normalizes first so
    an 8-digit ``YYYYMMDD`` argument is also accepted (mirroring ``_normalize_date``).
    """
    s = _normalize_date(date_str)
    d = datetime.strptime(s, "%Y-%m-%d")
    return (d + timedelta(days=1)).strftime("%Y-%m-%d")


def _row_for_date(df, requested: str):
    """Return the row (``pd.Series``) whose index date matches ``requested``.

    yfinance returns a date-indexed DataFrame, so the index is always the date
    axis. Matching tolerates 'YYYY-MM-DD' vs 'YYYYMMDD' on both sides.
    """
    values = [_normalize_date(v) for v in df.index.tolist()]
    target = _normalize_date(requested)
    for key in (target, target.replace("-", "")):
        if key in values:
            return df.iloc[values.index(key)]
    return None


class _YfinanceBase:
    """Shared param/value mechanics for yfinance Ticker-method adapters.

    Subclasses declare:
      ``_ALIASES``   - {binding column name -> physical column} so a concept
                       bound to a Chinese name (e.g. 收盘) resolves on the
                       English columns yfinance returns (Close);
      ``_RETRIES``/``_RETRY_DELAY`` - retry tuning for the optional ``call``.

    yfinance returns a date-indexed DataFrame, so there is no ``_DATE_COL``
    class attr — the index is always the date axis (unlike akshare, where some
    functions return the date as a named column).
    """

    _ALIASES: dict[str, str] = {}
    _RETRIES: int = DEFAULT_RETRIES
    _RETRY_DELAY: float = DEFAULT_RETRY_DELAY

    def extract_value(self, result: Any, column_name: str, date: str, identifier: Optional[str] = None) -> Any:
        """Pull the ``(date, column_name)`` cell from a yfinance result DataFrame.

        ``identifier`` (the symbol) is unused here; yfinance returns one symbol's
        series per call, so the date index alone picks the row.
        """
        import pandas as pd

        if not isinstance(result, pd.DataFrame) or result.empty:
            return None
        row = _row_for_date(result, date)
        if row is None:
            return None
        col = column_name if column_name in result.columns else self._ALIASES.get(column_name)
        if col is None or col not in result.columns:
            return None
        val = row[col]
        return None if pd.isna(val) else val

    def extract_series(self, result: Any, column_name: str, start: str, end: str) -> dict:
        """Pull every ``(date, column_name)`` cell with ``start <= date <= end``.

        Returns ``{'YYYY-MM-DD': value}`` (normalized dates, NaN cells skipped) —
        the batch form of ``extract_value`` used by ``read_range``.
        """
        import pandas as pd

        if not isinstance(result, pd.DataFrame) or result.empty:
            return {}
        col = column_name if column_name in result.columns else self._ALIASES.get(column_name)
        if col is None or col not in result.columns:
            return {}
        dates = [_normalize_date(v) for v in result.index.tolist()]
        out: dict[str, Any] = {}
        values = result[col].tolist()
        for d, val in zip(dates, values):
            if d and start <= d <= end and not pd.isna(val):
                out[d] = val
        return out

    def build_range_params(self, fn, identifier: str, start: str, end: str, binding=None) -> dict:
        """Range form of ``build_params``: ``start`` as-is, ``end`` advanced one day.

        yfinance ``end`` is exclusive, so the range's last day (``end``) must be
        advanced to be included. The single-date ``build_params`` already
        advanced ``end`` past ``start``; here we re-anchor ``end`` to
        ``_next_day(end)``.
        """
        params = self.build_params(fn, identifier, start, binding)
        params["end"] = _next_day(end)
        return params


# --- daily OHLCV ---------------------------------------------------------------

class TickerHistoryAdapter(_YfinanceBase):
    """``ticker_history`` -> ``yf.Ticker(symbol).history(start, end)``.

    Returns a date-indexed DataFrame with columns ``Open``/``High``/``Low``/
    ``Close``/``Volume``/``Adj Close``/``Dividends``/``Stock Splits``.

    ``build_params`` maps the requested single date to ``start=date`` +
    ``end=_next_day(date)`` (end is exclusive in yfinance). A concept bound to
    either the English column (``Close``) or the Chinese alias (``收盘``)
    resolves at extract time.
    """

    _ALIASES = {
        "日期": "Date", "开盘": "Open", "收盘": "Close", "最高": "High",
        "最低": "Low", "成交量": "Volume", "成交额": "Volume",
        "adj close": "Adj Close", "adjusted close": "Adj Close",
        "前复权": "Adj Close",
    }

    def build_params(self, fn, identifier: str, date: str, binding=None) -> dict:
        return {
            "symbol": identifier,
            "start": _normalize_date(date),
            "end": _next_day(date),
        }

    def call(self, command: str, params: dict) -> Any:
        """Invoke ``yf.Ticker(symbol).<method>(**rest)`` with a simple retry.

        Only used when ``fetch/runner.py`` opts in (a registered adapter with a
        ``call`` method). ``symbol`` is pulled out of ``params`` and used to
        build the ``Ticker``; every other kwarg (``start``/``end``/``period``/
        ``interval``/...) is passed through to the Ticker method unchanged, so
        both the adapter-built date-range params and ad-hoc caller params work.
        """
        import time

        import yfinance as yf

        from fd_open_data_mcp.fetch.runner import FetchError

        method = command[len("ticker_"):] if command.startswith("ticker_") else command
        symbol = params.get("symbol")
        if not symbol:
            raise FetchError(f"ticker_* command {command} needs a symbol")
        rest = {k: v for k, v in params.items() if k != "symbol"}
        ticker = yf.Ticker(symbol)
        attr = getattr(ticker, method, None)
        if attr is None or not callable(attr):
            raise FetchError(f"yfinance.Ticker has no method {method}")
        last_exc: Exception | None = None
        for attempt in range(self._RETRIES + 1):
            try:
                return attr(**rest)
            except Exception as exc:  # noqa: BLE001 - retry any transient upstream error
                last_exc = exc
                if attempt < self._RETRIES:
                    logger.debug(
                        "yfinance %s.%s attempt %d/%d failed (%s); retrying in %.1fs",
                        symbol, method, attempt + 2, self._RETRIES + 1, exc, self._RETRY_DELAY,
                    )
                    time.sleep(self._RETRY_DELAY)
        raise FetchError(
            f"yfinance {command} failed after {self._RETRIES + 1} attempts: {last_exc}"
        ) from last_exc


def register_all() -> None:
    """(Re)register all yfinance adapters (idempotent overwrite)."""
    register("yfinance", "ticker_history", TickerHistoryAdapter())


register_all()
