"""akshare per-function adapters, ported from ``scraw-akshare``'s fetch layer.

Each adapter implements the ``Adapter`` protocol (``build_params`` +
``extract_value``) with the REAL per-function param mapping and result-shape
handling, learned from ``scraw-akshare/scraw_akshare/akshare_client*.py`` and
``scraw-akshare/scripts/fetch_tencent.py`` (verified against akshare 1.18.64).
Adapters also expose an optional ``call(command, params)`` that wraps the
upstream callable in a native akshare ``timeout`` kwarg (where supported) plus a
simple retry for transient failures; ``fetch/runner.py`` opts into it when
present (task 2.3).

flaky-upstream notes (task 2.3 - what scraw-akshare actually had):
  * scraw-akshare has NO per-call timeout / retry / SIGALRM guard around its
    ``ak.*`` calls. The only retry/timeout lives in ``scraw_akshare/settings.py``
    as Scrapy downloader settings (``DOWNLOAD_TIMEOUT=60``, ``RETRY_TIMES=3``),
    which do NOT apply to the direct ``ak.*`` calls in ``akshare_client*.py``.
  * The SIGALRM ``extraction_timeout`` guard and the swsresearch direct API for
    申万 constituents live in ``scraw-fd-cn-report``, NOT scraw-akshare (grep of
    scraw-akshare finds neither ``SIGALRM``/``swsresearch``/``index_component_sw``).
  * scraw-akshare's real flaky-upstream workaround is SOURCE FAILOVER:
    eastmoney ``stock_zh_a_hist`` -> tencent ``stock_zh_a_hist_tx``
    (``scripts/fetch_tencent.py`` opens with "East Money API is blocked from this
    network"), and the em statement endpoints -> sina ``stock_financial_report_sina``.
    The dispatch's ranked failover already covers cross-source failover; the em->sina
    COMPOSITE fallback (inside scraw-akshare's fetch_* funcs) is NOT ported here
    (noted as a gap - a composite adapter would be needed).
  * The native ``timeout`` kwarg + ``call`` retry below are ADDED robustness
    (akshare supports ``timeout`` on ``stock_zh_a_hist`` / ``stock_zh_a_hist_tx``);
    scraw-akshare did not use them.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Optional

from fd_open_data_mcp.adapters import register

logger = logging.getLogger(__name__)

# --- tunables for the optional ``call`` retry/timeout (task 2.3) -----------------
DEFAULT_TIMEOUT: float = 30.0   # seconds; passed as akshare's native ``timeout`` kwarg
DEFAULT_RETRIES: int = 2        # extra attempts after the first failure
DEFAULT_RETRY_DELAY: float = 1.0  # seconds between retries


# --- date helpers (akshare returns date objects OR 'YYYY-MM-DD' / 'YYYYMMDD' str) --

def _compact(date_str: str) -> str:
    """Normalize a date string to akshare's YYYYMMDD form ('2024-07-26' -> '20240726')."""
    return str(date_str).replace("-", "")


def _normalize_date(value: Any) -> str:
    """Coerce a date cell to a canonical 'YYYY-MM-DD' string for matching.

    akshare returns python ``date``/``datetime`` for some columns (e.g.
    ``stock_zh_a_hist`` 日期, ``stock_zh_a_daily`` date) and 'YYYY-MM-DD' /
    'YYYYMMDD' strings for others; tolerate all so a requested date matches
    regardless of which side uses which form.
    """
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    s = str(value).strip()
    if len(s) == 8 and s.isdigit():  # 'YYYYMMDD' -> 'YYYY-MM-DD'
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s


def _row_for_date(df, date_col: Optional[str], requested: str):
    """Return the row (``pd.Series``) whose date matches ``requested``, or None.

    ``date_col`` is the column holding the date; when it is None, or when the
    declared column is absent from the DataFrame, the DataFrame index is treated
    as the date axis (e.g. some akshare versions return date as the index).
    Matching tolerates 'YYYY-MM-DD' vs 'YYYYMMDD' on both sides.
    """
    if date_col is not None and date_col in df.columns:
        values = [_normalize_date(v) for v in df[date_col].tolist()]
    else:
        values = [_normalize_date(v) for v in df.index.tolist()]
    target = _normalize_date(requested)
    for key in (target, target.replace("-", "")):
        if key in values:
            return df.iloc[values.index(key)]
    return None


# --- A-share code -> exchange prefix (ported from scraw-akshare) -----------------
# Tencent: scripts/fetch_tencent.py symbol_to_tx_prefix (6/5->sh, 0/3->sz, 4/8->bj).
def _tx_prefix(code: str) -> str:
    c = str(code).strip()
    if c.startswith(("6", "5")):
        return "sh"
    if c.startswith(("0", "3")):
        return "sz"
    if c.startswith(("4", "8")):
        return "bj"
    return "sh"

# Sina: akshare_client_fundamentals.py _sina_stock (6/9->sh, 8/4->bj, else sz).
def _sina_prefix(code: str) -> str:
    c = str(code).strip()
    if c.startswith(("6", "9")):
        return "sh"
    if c.startswith(("8", "4")):
        return "bj"
    return "sz"


class _AkshareBase:
    """Shared param/value mechanics for akshare adapters.

    Subclasses declare:
      ``_DATE_COL``  - the column holding the row date (None => date is the index);
      ``_ALIASES``   - {binding column name -> physical column name} for value
                       lookup, so a concept bound to the Chinese name (e.g. 收盘)
                       resolves on functions that return English columns (close);
      ``_TIMEOUT``   - native akshare ``timeout`` kwarg, or None when the function's
                       signature has no ``timeout`` parameter;
      ``_RETRIES``/``_RETRY_DELAY`` - retry tuning for the optional ``call``.
    """

    _DATE_COL: Optional[str] = None
    _ALIASES: dict[str, str] = {}
    _TIMEOUT: Optional[float] = None
    _RETRIES: int = DEFAULT_RETRIES
    _RETRY_DELAY: float = DEFAULT_RETRY_DELAY

    def extract_value(self, result: Any, column_name: str, date: str) -> Any:
        """Pull the ``(date, column_name)`` cell from an akshare result DataFrame."""
        import pandas as pd

        if not isinstance(result, pd.DataFrame) or result.empty:
            return None
        row = _row_for_date(result, self._DATE_COL, date)
        if row is None:
            return None
        # resolve the requested column: exact name first, then alias, else give up
        col = column_name if column_name in result.columns else self._ALIASES.get(column_name)
        if col is None or col not in result.columns:
            return None
        val = row[col]
        return None if pd.isna(val) else val

    def call(self, command: str, params: dict) -> Any:
        """Invoke ``akshare.<command>(**params)`` with a native timeout + simple retry.

        Only used when ``fetch/runner.py`` opts in (a registered adapter with a
        ``call`` method). ``build_params`` already injects the ``timeout`` kwarg
        for functions that support one (``_TIMEOUT`` is not None), so the legacy
        direct-call path also gets the timeout even without this retry wrapper.
        """
        import time

        import akshare as ak

        from fd_open_data_mcp.fetch.runner import FetchError

        fn = getattr(ak, command, None)
        if fn is None or not callable(fn):
            raise FetchError(f"akshare has no callable {command}")
        last_exc: Exception | None = None
        for attempt in range(self._RETRIES + 1):
            try:
                return fn(**params)
            except Exception as exc:  # noqa: BLE001 - retry any transient upstream error
                last_exc = exc
                if attempt < self._RETRIES:
                    logger.debug(
                        "akshare %s attempt %d/%d failed (%s); retrying in %.1fs",
                        command, attempt + 2, self._RETRIES + 1, exc, self._RETRY_DELAY,
                    )
                    time.sleep(self._RETRY_DELAY)
        raise FetchError(
            f"akshare {command} failed after {self._RETRIES + 1} attempts: {last_exc}"
        ) from last_exc


# --- daily OHLCV ----------------------------------------------------------------

class StockZhAHistAdapter(_AkshareBase):
    """``ak.stock_zh_a_hist`` - East Money daily/weekly/monthly OHLCV (main path).

    Ported from ``scraw-akshare/scraw_akshare/akshare_client.py`` (verified
    akshare 1.18.64). Signature: ``(symbol, period, start_date, end_date, adjust,
    timeout)`` where:
      * ``symbol``: bare A-share code, e.g. ``'600000'`` (NO exchange prefix);
      * ``period``: ``'daily'``/``'weekly'``/``'monthly'``;
      * ``start_date``/``end_date``: ``'YYYYMMDD'`` inclusive;
      * ``adjust``: ``''`` (none) / ``'qfq'`` / ``'hfq'`` - scraw-akshare default ``'qfq'``;
      * ``timeout``: native request timeout (akshare supports it; scraw-akshare did not).
    Returns 12 Chinese columns: 日期, 股票代码, 开盘, 收盘, 最高, 最低, 成交量,
    成交额, 振幅, 涨跌幅, 涨跌额, 换手率. Date column = 日期.
    """

    _DATE_COL = "日期"
    _ALIASES = {}  # binding column names are Chinese -> direct lookup
    _TIMEOUT = DEFAULT_TIMEOUT

    def build_params(self, fn, identifier: str, date: str, binding=None) -> dict:
        return {
            "symbol": identifier,
            "period": "daily",
            "start_date": _compact(date),
            "end_date": _compact(date),
            "adjust": "qfq",
            "timeout": self._TIMEOUT,
        }


class StockZhAHistTxAdapter(_AkshareBase):
    """``ak.stock_zh_a_hist_tx`` - Tencent QQ finance daily OHLCV (eastmoney failover).

    Ported from ``scraw-akshare/scripts/fetch_tencent.py``. Signature:
    ``(symbol, start_date, end_date, adjust, timeout)`` where:
      * ``symbol``: WITH Tencent market prefix, e.g. ``'sh600000'``
        (6/5->sh, 0/3->sz, 4/8->bj);
      * ``start_date``/``end_date``: ``'YYYYMMDD'``; ``adjust``: ``'qfq'``/``'hfq'``/``''``;
      * no ``period`` (always daily).
    Returns English columns: date, open, high, low, close, amount (volume is NOT
    provided by Tencent - scraw-akshare set it to None). Date column = date.
    """

    _DATE_COL = "date"
    _ALIASES = {  # concept bound to Chinese name -> physical English column
        "日期": "date", "开盘": "open", "收盘": "close", "最高": "high",
        "最低": "low", "成交量": "volume", "成交额": "amount",
    }
    _TIMEOUT = DEFAULT_TIMEOUT

    def build_params(self, fn, identifier: str, date: str, binding=None) -> dict:
        return {
            "symbol": _tx_prefix(identifier) + str(identifier),
            "start_date": _compact(date),
            "end_date": _compact(date),
            "adjust": "qfq",
            "timeout": self._TIMEOUT,
        }


class StockZhADailyAdapter(_AkshareBase):
    """``ak.stock_zh_a_daily`` - Sina daily OHLCV.

    NOTE: scraw-akshare does NOT use this function (it uses ``stock_zh_a_hist`` /
    ``stock_zh_a_hist_tx``); the mapping below is from akshare 1.18.64's
    introspected signature + source columns (``akshare/stock/stock_zh_a_sina.py``),
    NOT from scraw-akshare. Signature: ``(symbol, start_date, end_date, adjust)``
    where:
      * ``symbol``: WITH Sina market prefix, e.g. ``'sh600000'`` (6/9->sh, 8/4->bj, else sz);
      * ``start_date``/``end_date``: ``'YYYYMMDD'``; ``adjust``: ``'qfq'``/``'hfq'``/``''``;
      * no native ``timeout`` kwarg (the Sina endpoint does not accept one).
    Returns a DataFrame with a ``date`` column (python ``date`` objects) and
    English columns: open, high, low, close, volume, amount, outstanding_share,
    turnover. (``_DATE_COL`` is 'date'; the index-fallback in ``_row_for_date``
    covers akshare versions that return date as the index instead.)
    """

    _DATE_COL = "date"
    _ALIASES = {
        "日期": "date", "开盘": "open", "收盘": "close", "最高": "high",
        "最低": "low", "成交量": "volume", "成交额": "amount",
    }
    _TIMEOUT = None  # Sina endpoint has no timeout kwarg

    def build_params(self, fn, identifier: str, date: str, binding=None) -> dict:
        return {
            "symbol": _sina_prefix(identifier) + str(identifier),
            "start_date": _compact(date),
            "end_date": _compact(date),
            "adjust": "qfq",
        }


# --- financial statements (quarterly; the requested ``date`` is a report period) --

class StockFinancialIndicatorAdapter(_AkshareBase):
    """``ak.stock_financial_analysis_indicator`` - Sina financial indicators.

    Ported from ``scraw-akshare/scraw_akshare/akshare_client_fundamentals.py``.
    Signature: ``(symbol, start_year)`` where ``symbol`` is a bare A-share code
    (``'600000'``) and ``start_year`` is ``'YYYY'`` (derived from the requested
    date's year). Date column = 日期; columns are Chinese indicator names
    (净资产收益率(%), 每股收益(元), ...).
    """

    _DATE_COL = "日期"
    _ALIASES = {}
    _TIMEOUT = None

    def build_params(self, fn, identifier: str, date: str, binding=None) -> dict:
        return {"symbol": identifier, "start_year": str(date)[:4]}


class _EmStatementAdapter(_AkshareBase):
    """Common base for the 东财 (eastmoney) report-by-report statement endpoints.

    Each signature is ``(symbol)`` - returns ALL report periods for the symbol.
    scraw-akshare passes the BARE code (e.g. ``'600000'``); the akshare signature
    default ``'SH600519'`` suggests a prefixed form is also accepted. scraw-akshare
    wrapped these in try/except with a Sina fallback (``stock_financial_report_sina``);
    that COMPOSITE fallback is NOT ported here (gap - needs a composite adapter).
    Date column = 报告期; the requested ``date`` is a report period (e.g. 2024-06-30).
    """

    _DATE_COL = "报告期"
    _ALIASES = {}
    _TIMEOUT = None

    def build_params(self, fn, identifier: str, date: str, binding=None) -> dict:
        return {"symbol": identifier}


class StockProfitSheetAdapter(_EmStatementAdapter):
    """``ak.stock_profit_sheet_by_report_em`` - 利润表 (profit statement).

    Columns incl. 报告期, 营业总收入, 净利润, 归属于母公司股东的净利润, 基本每股收益, ...
    """


class StockBalanceSheetAdapter(_EmStatementAdapter):
    """``ak.stock_balance_sheet_by_report_em`` - 资产负债表 (balance sheet).

    Columns incl. 报告期, 资产总计, 货币资金, 存货, 负债合计, 股东权益合计, ...
    """


class StockCashFlowSheetAdapter(_EmStatementAdapter):
    """``ak.stock_cash_flow_sheet_by_report_em`` - 现金流量表 (cash flow statement).

    Columns incl. 报告期, 经营活动产生的现金流量净额, 投资活动产生的现金流量净额, ...
    """


# --- registration (import-time; register() is an idempotent overwrite) ----------
def register_all() -> None:
    """Register all built-in akshare adapters (idempotent).

    Called at import time below; also callable from tests to re-populate the
    registry after a ``_REGISTRY.clear()`` (avoids the class-identity breakage
    that ``importlib.reload`` would cause).
    """
    register("akshare", "stock_zh_a_hist", StockZhAHistAdapter())
    register("akshare", "stock_zh_a_hist_tx", StockZhAHistTxAdapter())
    register("akshare", "stock_zh_a_daily", StockZhADailyAdapter())
    register("akshare", "stock_financial_analysis_indicator", StockFinancialIndicatorAdapter())
    register("akshare", "stock_profit_sheet_by_report_em", StockProfitSheetAdapter())
    register("akshare", "stock_balance_sheet_by_report_em", StockBalanceSheetAdapter())
    register("akshare", "stock_cash_flow_sheet_by_report_em", StockCashFlowSheetAdapter())


register_all()
