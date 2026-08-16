"""edgar per-function adapters.

edgartools imports as ``edgar``; its surface is the ``Company`` object (like
yfinance's ``Ticker``). Commands are flattened to ``company_<method>`` by
``catalog/upstream.py:introspect_edgar`` (the primary dated command is
``company_get_filings`` <- ``edgar.Company(ticker).get_filings()``).

Unlike yfinance (which returns a date-indexed DataFrame), ``get_filings()``
returns a **``CompanyFilings`` object** (NOT a DataFrame) with:
  * ``.data`` - a pyarrow table of filing rows (form, filingDate, accession_no, ...);
  * ``.filter(form=..., filing_date=...)`` - date filter supports single date
    (``"2023-02-23"``), range (``"start:end"``), before (``":end"``), after
    (``"start:"``); ``date`` and ``filing_date`` are interchangeable;
  * ``.latest()`` - the most recent filing.

So the adapter's ``call()`` filters by ``filing_date`` and returns the (narrowed)
``CompanyFilings``; ``extract_value`` coerces it to a pandas DataFrame via
``.data.to_pandas()`` for uniform extraction. The coercion also covers the legacy
path (``run_edgar`` with NO adapter -> raw ``CompanyFilings`` with no filter),
so both paths share one extraction code path.

Quirks handled here (learned from edgartools, NOT from a scraw project - edgar
had no scraw crawler):
  * The SEC requires a ``User-Agent`` identity (``EDGAR_IDENTITY`` env var);
    ``call()`` triggers ``_ensure_edgar_identity()`` before any fetch.
  * ``filing_date`` is the date axis (not a price axis like yfinance); a requested
    date filters to filings filed on that date, and ``extract_value`` returns the
    first matching filing's column value.
  * The pyarrow ``.data`` column names vary by edgartools version (``filingDate``
    vs ``filing_date``); ``_find_date_col`` probes candidates rather than
    hardcoding one.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Optional

from fd_open_data_mcp.adapters import register

logger = logging.getLogger(__name__)

# --- tunables for the optional ``call`` retry (mirror yfinance) -----------------
DEFAULT_RETRIES: int = 2          # extra attempts after the first failure
DEFAULT_RETRY_DELAY: float = 1.0  # seconds between retries

# Candidate date-column names in the CompanyFilings pyarrow table (edgartools
# has renamed these across versions; probe rather than hardcode).
_DATE_COL_CANDIDATES = ("filing_date", "filingDate", "filing date", "date")


# --- date helpers (edgar returns 'YYYY-MM-DD' strings / datetimes in .data) -------

def _normalize_date(value: Any) -> str:
    """Coerce a date cell to a canonical 'YYYY-MM-DD' string for matching.

    edgar's pyarrow ``.data`` holds filing dates as strings ('YYYY-MM-DD' or a
    full timestamp) or datetimes; tolerate all so a requested date matches
    regardless of which form the cell uses. Bare strings are truncated to the
    10-char ISO date so a full Timestamp repr also matches.
    """
    if value is None:
        return ""
    if isinstance(value, datetime):  # also covers pandas.Timestamp
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    s = str(value).strip()
    # tolerate 8-digit YYYYMMDD strings by inserting dashes
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s[:10] if len(s) >= 10 else s


def _to_dataframe(result: Any):
    """Coerce a ``CompanyFilings``-like result to a pandas DataFrame.

    Returns the input unchanged if it is already a DataFrame (adapter ``call()``
    does NOT pre-convert - it returns the CompanyFilings; ``extract_value`` does
    the conversion so both adapter and legacy paths share one code path). For a
    ``CompanyFilings``-like object, extracts ``.data`` (a pyarrow table) and
    converts via ``.to_pandas()``. Returns ``None`` if the shape is unrecognized.
    """
    import pandas as pd

    if isinstance(result, pd.DataFrame):
        return result
    data = getattr(result, "data", None)
    if data is not None:
        to_pandas = getattr(data, "to_pandas", None)
        if callable(to_pandas):
            try:
                return to_pandas()
            except Exception:  # noqa: BLE001 - fall through to None
                pass
    return None


class _EdgarBase:
    """Shared param/value mechanics for edgar Company-method adapters.

    Subclasses declare:
      ``_ALIASES``   - {binding column name -> physical column} so a concept
                       bound to one name resolves on the physical column the
                       pyarrow table actually returns;
      ``_RETRIES``/``_RETRY_DELAY`` - retry tuning for the optional ``call``.

    The date axis is a named column in the ``.data`` table (probed by
    ``_find_date_col``), NOT the DataFrame index (unlike yfinance).
    """

    _ALIASES: dict[str, str] = {}
    _RETRIES: int = DEFAULT_RETRIES
    _RETRY_DELAY: float = DEFAULT_RETRY_DELAY

    def _find_date_col(self, df) -> Optional[str]:
        for cand in _DATE_COL_CANDIDATES:
            if cand in df.columns:
                return cand
        return None

    def extract_value(self, result: Any, column_name: str, date: str, identifier: Optional[str] = None) -> Any:
        """Pull the ``(date, column_name)`` cell from an edgar filings result.

        ``identifier`` (the ticker) is unused here; one company's filings are
        returned per call, so the filing date alone picks the row. Coerces a
        ``CompanyFilings`` to a DataFrame first (no-op for an already-converted
        frame), so the adapter path (``call()`` returns CompanyFilings) and the
        legacy path (``run_edgar`` returns CompanyFilings with no filter) share
        one extraction code path.
        """
        import pandas as pd

        df = _to_dataframe(result)
        if df is None or df.empty:
            return None
        # resolve the requested column: exact name first, then alias, else give up
        col = column_name if column_name in df.columns else self._ALIASES.get(column_name)
        if col is None or col not in df.columns:
            return None
        # match the requested filing date (the date axis is a named column)
        date_col = self._find_date_col(df)
        target = _normalize_date(date)
        if date_col is not None:
            values = [_normalize_date(v) for v in df[date_col].tolist()]
            for key in (target, target.replace("-", "")):
                if key in values:
                    val = df.iloc[values.index(key)][col]
                    return None if pd.isna(val) else val
            return None
        # no date column recognizable -> take the first row (e.g. a .latest() snapshot)
        val = df.iloc[0][col]
        return None if pd.isna(val) else val

    def extract_series(self, result: Any, column_name: str, start: str, end: str) -> dict:
        """Pull every ``(date, column_name)`` cell with ``start <= date <= end``.

        Returns ``{'YYYY-MM-DD': value}`` (normalized dates, NaN cells skipped) —
        the batch form of ``extract_value`` used by ``read_range``.
        """
        import pandas as pd

        df = _to_dataframe(result)
        if df is None or df.empty:
            return {}
        col = column_name if column_name in df.columns else self._ALIASES.get(column_name)
        if col is None or col not in df.columns:
            return {}
        date_col = self._find_date_col(df)
        if date_col is None:
            return {}
        dates = [_normalize_date(v) for v in df[date_col].tolist()]
        out: dict[str, Any] = {}
        values = df[col].tolist()
        for d, val in zip(dates, values):
            if d and start <= d <= end and not pd.isna(val):
                out[d] = val
        return out

    def build_range_params(self, fn, identifier: str, start: str, end: str, binding=None) -> dict:
        """Range form of ``build_params``: ``filing_date`` becomes ``"start:end"``.

        edgar's ``filter(filing_date="start:end")`` selects filings filed within
        the inclusive range, so the single-date ``build_params`` (which sets
        ``filing_date`` to the start date) is rebuilt and its ``filing_date``
        overridden with the range form.
        """
        params = self.build_params(fn, identifier, start, binding)
        params["filing_date"] = f"{_normalize_date(start)}:{_normalize_date(end)}"
        return params


# --- filings -------------------------------------------------------------------

class CompanyGetFilingsAdapter(_EdgarBase):
    """``company_get_filings`` -> ``edgar.Company(ticker).get_filings().filter(filing_date=date)``.

    Returns a ``CompanyFilings`` object; ``call()`` filters by the requested
    filing date (when provided) and returns the narrowed ``CompanyFilings``;
    ``extract_value`` then coerces it to a DataFrame and pulls the first matching
    row's column value.

    ``build_params`` maps the requested single date to ``ticker`` + ``filing_date``
    (an ISO 'YYYY-MM-DD' string; edgar's ``filter(filing_date=...)`` accepts it).
    A concept bound to a filing column (``form`` / ``accession_no`` / ...) resolves
    at extract time.
    """

    _ALIASES = {}  # binding column names are the physical pyarrow column names

    def build_params(self, fn, identifier: str, date: str, binding=None) -> dict:
        return {"ticker": identifier, "filing_date": _normalize_date(date)}

    def call(self, command: str, params: dict) -> Any:
        """Invoke ``edgar.Company(ticker).get_filings().filter(filing_date=...)`` with a retry.

        Only used when ``fetch/runner.py`` opts in (a registered adapter with a
        ``call`` method). ``ticker`` is pulled out of ``params`` and used to build
        the ``Company``; ``filing_date`` (when present) narrows the filings via
        ``.filter(filing_date=...)``. The raw ``CompanyFilings`` is returned —
        ``extract_value`` does the DataFrame coercion.
        """
        import time

        import edgar

        from fd_open_data_mcp.fetch.runner import FetchError, _ensure_edgar_identity

        _ensure_edgar_identity()
        ticker = params.get("ticker")
        if not ticker:
            raise FetchError(f"company_* command {command} needs a ticker")
        filing_date = params.get("filing_date")
        method = command[len("company_"):] if command.startswith("company_") else command
        company = edgar.Company(ticker)
        attr = getattr(company, method, None)
        if attr is None or not callable(attr):
            raise FetchError(f"edgar.Company has no method {method}")
        last_exc: Exception | None = None
        for attempt in range(self._RETRIES + 1):
            try:
                result = attr()
                if filing_date and hasattr(result, "filter") and hasattr(result, "data"):
                    result = result.filter(filing_date=filing_date)
                return result
            except Exception as exc:  # noqa: BLE001 - retry any transient upstream error
                last_exc = exc
                if attempt < self._RETRIES:
                    logger.debug(
                        "edgar Company.%s attempt %d/%d failed (%s); retrying in %.1fs",
                        method, attempt + 2, self._RETRIES + 1, exc, self._RETRY_DELAY,
                    )
                    time.sleep(self._RETRY_DELAY)
        raise FetchError(
            f"edgar {command} failed after {self._RETRIES + 1} attempts: {last_exc}"
        ) from last_exc


def register_all() -> None:
    """(Re)register all edgar adapters (idempotent overwrite)."""
    register("edgar", "company_get_filings", CompanyGetFilingsAdapter())


register_all()
