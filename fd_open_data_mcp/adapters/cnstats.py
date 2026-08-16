"""cnstats per-function adapters — Chinese NBS macro indicators via akshare.

cnstats is **not** a separate library; the 8 curated NBS macro indicators
(CPI, PMI, industrial output, fixed-asset investment, retail sales, GDP,
trade balance, money supply) are backed by **akshare** macro functions
(``macro_china_cpi_yearly`` etc., per the DAAS dispatch.py parity target). It
is **keyless** (no env var, no API key — akshare fetches the NBS-published
macro series directly) and takes no per-call params: each macro function
returns the full monthly/quarterly national series, so ``build_params``
returns ``{}`` and the entity identifier is unused (macro data is
country-level, not per-entity).

The akshare macro functions return a ``pandas.DataFrame`` with a 日期 (date)
axis and Chinese-named value columns (verbatim shapes recorded in
``catalog/seeds/cnstats.py``). This adapter mirrors the akshare
``_AkshareBase`` date-axis helpers (``_normalize_date`` / ``_row_for_date``)
for ``extract_value`` / ``extract_series`` — cnstats IS timeseries macro data
(unlike ckan, which is discovery metadata with NO date axis and therefore
returns ``{}`` from ``extract_series``). The fd-world reference
(``fd_world/sources/cnstats_source.py``) had a ``try: return func() except
Exception: return func`` quirk that returned the bare callable on failure; this
adapter instead calls with a retry loop and raises ``FetchError`` on
persistent failure (the ckan/dartlab pattern).
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Optional

from fd_open_data_mcp.adapters import register

logger = logging.getLogger(__name__)

# --- tunables for the optional ``call`` retry (mirrors ckan/dartlab) -------------
DEFAULT_RETRIES: int = 2        # extra attempts after the first failure
DEFAULT_RETRY_DELAY: float = 1.0  # seconds between retries

# cnstats command -> akshare macro function name.
# Verbatim from ``fd_world/sources/cnstats_source.py`` CNStatsAdapter.fetch()
# mapping + the DAAS ``dispatch.py`` ``cnstats_`` entry. The command names
# here are **unprefixed** (``cpi`` not ``cnstats_cpi``) — the adapter/runner
# convention — and each maps to exactly one akshare macro function.
MAPPING: dict[str, str] = {
    "cpi": "macro_china_cpi_yearly",
    "pmi": "macro_china_pmi",
    "industrial_output": "macro_china_industrial_production_yoy",
    "fixed_asset_investment": "macro_china_fixed_asset_investment",
    "retail_sales": "macro_china_consumer_goods_retail",
    "gdp_quarterly": "macro_china_gdp_yearly",
    "trade_balance": "macro_china_trade_balance",
    "money_supply": "macro_china_money_supply",
}


# --- date helpers (akshare macro frames carry a 日期 column or a date index) -----

def _normalize_date(value: Any) -> str:
    """Coerce a date cell to a canonical 'YYYY-MM-DD' string for matching.

    akshare macro functions return python ``date``/``datetime`` or
    'YYYY-MM-DD' / 'YYYYMMDD' strings for the 日期 column; tolerate all so a
    requested date matches regardless of which side uses which form. (Mirrors
    the akshare adapter's ``_normalize_date``.)
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

    ``date_col`` is the column holding the date (``日期`` for cnstats); when it
    is None or absent from the DataFrame, the DataFrame index is treated as the
    date axis (some akshare macro versions return date as the index).
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


class _CnstatsBase:
    """Shared mechanics for all 8 cnstats macro commands.

    All commands are keyless (no env var) and take no per-call params — the
    akshare macro functions return the full monthly/quarterly series, so
    ``build_params`` returns ``{}`` and the entity identifier is unused. The
    only per-command difference is the akshare function name, looked up via
    ``MAPPING`` inside ``call()``. A single shared instance is registered under
    all 8 commands (identical mechanics → no per-command subclass needed).
    """

    _DATE_COL: Optional[str] = "日期"
    _RETRIES: int = DEFAULT_RETRIES
    _RETRY_DELAY: float = DEFAULT_RETRY_DELAY

    def build_params(self, fn, identifier: str, date: str, binding=None) -> dict:
        """No params — akshare macro functions take no arguments."""
        return {}

    def build_range_params(self, fn, identifier: str, start: str, end: str, binding=None) -> dict:
        """Range form: same as ``build_params`` (no date params upstream)."""
        return {}

    def extract_value(self, result: Any, column_name: str, date: str,
                      identifier: Optional[str] = None) -> Any:
        """Pull the ``(date, column_name)`` cell from a macro DataFrame.

        ``identifier`` is unused (macro data is country-level); ``date`` drives
        the row pick via the 日期 column (or index fallback).
        """
        import pandas as pd

        if not isinstance(result, pd.DataFrame) or result.empty:
            return None
        row = _row_for_date(result, self._DATE_COL, date)
        if row is None:
            return None
        # cnstats columns are Chinese-named and bound directly (no aliases).
        if column_name not in result.columns:
            return None
        val = row[column_name]
        return None if pd.isna(val) else val

    def extract_series(self, result: Any, column_name: str, start: str, end: str) -> dict:
        """Pull every ``(date, column_name)`` cell with ``start <= date <= end``.

        Returns ``{'YYYY-MM-DD': value}`` (normalized dates, NaN cells skipped) —
        the batch form of ``extract_value`` used by ``read_range``.
        """
        import pandas as pd

        if not isinstance(result, pd.DataFrame) or result.empty:
            return {}
        if column_name not in result.columns:
            return {}
        if self._DATE_COL is not None and self._DATE_COL in result.columns:
            dates = [_normalize_date(v) for v in result[self._DATE_COL].tolist()]
        else:
            dates = [_normalize_date(v) for v in result.index.tolist()]
        out: dict[str, Any] = {}
        values = result[column_name].tolist()
        for d, val in zip(dates, values):
            if d and start <= d <= end and not pd.isna(val):
                out[d] = val
        return out

    def call(self, command: str, params: dict) -> Any:
        """Invoke the mapped akshare macro function with a simple retry.

        ``params`` is ``{}`` (macro functions take no args). Lazy-imports
        akshare (already a hard dep via the akshare adapter), looks up the
        mapped function per ``MAPPING``, retries transient failures, and
        raises ``FetchError`` after exhaustion — NOT the fd-world
        ``try: return func() except: return func`` quirk.
        """
        import time

        from fd_open_data_mcp.fetch.runner import FetchError

        # Validate the command before importing akshare — the mapping lookup is
        # pure dict work and doesn't need the upstream lib, so an unknown command
        # raises FetchError (not ModuleNotFoundError) in environments without
        # akshare installed.
        mapped = MAPPING.get(command)
        if not mapped:
            raise FetchError(f"cnstats has no mapping for command {command}")

        import akshare as ak  # lazy; hard dep via the akshare adapter

        fn = getattr(ak, mapped, None)
        if fn is None or not callable(fn):
            raise FetchError(
                f"akshare has no callable {mapped} (cnstats {command})"
            )
        last_exc: Exception | None = None
        for attempt in range(self._RETRIES + 1):
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001 - retry any transient upstream error
                last_exc = exc
                if attempt < self._RETRIES:
                    logger.debug(
                        "cnstats %s attempt %d/%d failed (%s); retrying in %.1fs",
                        command, attempt + 2, self._RETRIES + 1, exc, self._RETRY_DELAY,
                    )
                    time.sleep(self._RETRY_DELAY)
        raise FetchError(
            f"cnstats {command} failed after {self._RETRIES + 1} attempts: {last_exc}"
        ) from last_exc


# --- registration (import-time; register() is an idempotent overwrite) ----------
def register_all() -> None:
    """Register all cnstats adapters (idempotent).

    A single shared ``_CnstatsBase`` instance is registered under all 8
    commands (identical mechanics → no per-command subclass needed). Called at
    import time below; also callable from tests to re-populate the registry
    after a ``_REGISTRY.clear()``.
    """
    base = _CnstatsBase()
    for command in MAPPING:
        register("cnstats", command, base)


register_all()
