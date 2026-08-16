"""dartlab (PyPI dist ``dartlab``) adapters.

dartlab imports as ``dartlab``; requires **Python 3.12**. Its Korean
corporate-data API is centred on ``Company`` — a **factory function** (not a
class) that routes via a provider ``canHandle`` chain:

  - ``dartlab.Company(code)`` accepts 종목코드 ("005930"), 회사명 ("삼성전자"),
    or a US ticker ("AAPL"); DART→EDGAR auto-routing. Returns a company proxy.
  - ``company.panel`` is a **property** returning a ``Panel`` (a ``pl.DataFrame``
    subclass). ``panel(key=None, freq=...)`` returns the wide accounting grid
    (항목 rows × period columns named like "2025Q4"/"2024"); ``key=None`` → the
    full grid. ``None`` on no match (never raises).
  - ``company.credit`` / ``company.analysis`` are likewise **callable
    properties**: ``credit("등급")`` / ``credit(detail=True)`` → dict
    (grade/score/healthScore/axes/outlook); ``analysis()`` → guide,
    ``analysis("financial", "수익성")`` → dict | pl.DataFrame (22 axes, 5 groups).
  - ``company.news(*, days=30)`` → pl.DataFrame (title/date/source/link),
    keyless (public RSS).
  - ``company.disclosure(start, end, *, type=None, keyword=None, finalOnly=False)``
    → pl.DataFrame (docId/filedAt/title/formType); **requires DART_API_KEY**.
  - ``Company.search(keyword, *, limit=None)`` staticmethod → pl.DataFrame
    (stockCode/corpName/market/sector), keyless (KIND listing).

Results are **polars** DataFrames (coerced to pandas via ``.to_pandas()``);
credit/analysis return **dicts**. The wide panel is 항목 (account) rows × period
columns — extraction matches the account name against a label column (row) and
the requested date against a period header (column).

The DART credential is ``DART_API_KEY`` (alt ``DART_API_KEYS``, comma-separated,
90-day expiry), read **directly** by dartlab — the guard below is a presence
check only, with NO ``configure()`` call (unlike edinet-tools, which needs
``edinet_tools.configure(api_key=...)``).
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from fd_open_data_mcp.adapters import register

logger = logging.getLogger(__name__)

DEFAULT_RETRIES = 2
DEFAULT_RETRY_DELAY = 1.0

# ---------------------------------------------------------------------------
# Date / period helpers
# ---------------------------------------------------------------------------

# Long-frame date-axis candidates (news/disclosure).
_DATE_COL_CANDIDATES = (
    "date", "disclosure_date", "rcept_dt", "published",
    "datetime", "filing_date", "filedAt",
)

# Wide-panel label (row-identity) column candidates — the account/item name.
_LABEL_COL_CANDIDATES = (
    "canonicalKey", "sectionLeaf", "blockLeaf",
    "item", "항목", "account", "name", "label",
)

# Period headers: "2025Q4", "2024", "2024-12", "2024-Q4".
_PERIOD_RE = re.compile(r"^(\d{4})(?:[-\s]?(Q[1-4]))?(?:-(\d{2}))?$")
_QUARTER_END = {"Q1": "03-31", "Q2": "06-30", "Q3": "09-30", "Q4": "12-31"}


def _normalize_date(value) -> Optional[str]:
    """Coerce datetime/date/Timestamp/'YYYYMMDD'/'YYYY-MM-DD...' to 'YYYY-MM-DD'."""
    if value is None:
        return None
    import datetime as _dt

    if isinstance(value, _dt.datetime):
        return value.date().isoformat()
    if isinstance(value, _dt.date):
        return value.isoformat()
    text = str(value).strip()
    if not text:
        return None
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text[:10]


def _find_date_col(df) -> Optional[str]:
    for candidate in _DATE_COL_CANDIDATES:
        if candidate in df.columns:
            return candidate
    return None


def _is_period_col(col: str) -> bool:
    """True for wide-panel period headers like '2025Q4', '2024', '2024-12'."""
    return bool(_PERIOD_RE.match(str(col)))


def _period_to_date(header) -> Optional[str]:
    """'2025Q4' → '2025-12-31', '2024' → '2024-12-31', '2024-12' → '2024-12-31'."""
    m = _PERIOD_RE.match(str(header))
    if not m:
        return None
    year, quarter, month = m.group(1), m.group(2), m.group(3)
    if quarter:
        return f"{year}-{_QUARTER_END[quarter]}"
    if month:
        return f"{year}-{month}"
    # year-only → fiscal year end (Korea: Dec)
    return f"{year}-12-31"


def _period_matches(header, target: Optional[str]) -> bool:
    """Does period header resolve to ``target`` ('YYYY-MM-DD')?"""
    if target is None:
        return False
    d = _period_to_date(header)
    if d == target:
        return True
    # year-only header matches a Q4 target of the same year.
    if d and len(d) == 10 and d[5:] == "12-31" and target[5:] == "12-31" and d[:4] == target[:4]:
        return True
    return False


# ---------------------------------------------------------------------------
# Coercion
# ---------------------------------------------------------------------------

def _to_dataframe(result) -> Any:
    """Coerce polars/list[dict] to a pandas DataFrame; passthrough for pandas.

    Returns ``None`` for dicts (handled by the dict extraction path), unknown
    shapes, or empty frames.
    """
    import pandas as pd

    if result is None:
        return None
    if isinstance(result, pd.DataFrame):
        return result
    try:
        import polars as pl

        if isinstance(result, pl.DataFrame):
            if result.is_empty():
                return None
            return result.to_pandas()
    except ImportError:
        pass
    if isinstance(result, (list, tuple)):
        rows = [r for r in result if isinstance(r, dict)]
        if not rows:
            return None
        return pd.DataFrame(rows)
    return None


# ---------------------------------------------------------------------------
# Extraction strategies (module-level so adapters compose without MRO clashes)
# ---------------------------------------------------------------------------

def _long_extract_value(result, column_name, date, aliases) -> Any:
    """Long-frame (row-per-observation) value extraction, mirroring edinet."""
    df = _to_dataframe(result)
    if df is None or getattr(df, "empty", True):
        return None
    col = column_name if column_name in df.columns else aliases.get(column_name)
    if col is None or col not in df.columns:
        return None
    target = _normalize_date(date)
    date_col = _find_date_col(df)
    if date_col is not None and target is not None:
        target_compact = target.replace("-", "")
        for _, row in df.iterrows():
            rd = _normalize_date(row[date_col])
            if rd is None:
                continue
            if rd == target or rd.replace("-", "") == target_compact:
                return row[col]
        return None
    # No date axis to match on: fall back to the first row.
    return df.iloc[0][col]


def _long_extract_series(result, column_name, start, end, aliases) -> dict:
    df = _to_dataframe(result)
    if df is None or getattr(df, "empty", True):
        return {}
    col = column_name if column_name in df.columns else aliases.get(column_name)
    if col is None or col not in df.columns:
        return {}
    date_col = _find_date_col(df)
    if date_col is None:
        return {}
    start_n = _normalize_date(start)
    end_n = _normalize_date(end)
    out = {}
    for _, row in df.iterrows():
        d = _normalize_date(row[date_col])
        if d is None:
            continue
        if start_n and d < start_n:
            continue
        if end_n and d > end_n:
            continue
        out[d] = row[col]
    return out


def _find_label_col(df) -> Optional[str]:
    for c in _LABEL_COL_CANDIDATES:
        if c in df.columns:
            return c
    return None


def _match_label_row(df, label_col, column_name):
    """Return the first row whose label col matches column_name (exact→CI→substring)."""
    if label_col is None:
        return None
    series = df[label_col].astype(str)
    needle = str(column_name)
    mask = series == needle
    if not mask.any():
        mask = series.str.lower() == needle.lower()
    if not mask.any():
        mask = series.str.contains(needle, case=False, na=False, regex=False)
    if not mask.any():
        return None
    return df[mask].iloc[0]


def _wide_extract_value(result, column_name, date) -> Any:
    """Wide-panel value: column_name → row (label col), date → period column."""
    df = _to_dataframe(result)
    if df is None or getattr(df, "empty", True):
        return None
    label_col = _find_label_col(df)
    period_cols = [c for c in df.columns if _is_period_col(c)]
    row = _match_label_row(df, label_col, column_name)
    if row is None:
        return None
    target = _normalize_date(date)
    if target is not None and period_cols:
        for col in period_cols:
            if _period_matches(col, target):
                return row[col]
        return None
    if period_cols:
        return row[period_cols[0]]
    return None


def _wide_extract_series(result, column_name, start, end) -> dict:
    df = _to_dataframe(result)
    if df is None or getattr(df, "empty", True):
        return {}
    label_col = _find_label_col(df)
    period_cols = [c for c in df.columns if _is_period_col(c)]
    row = _match_label_row(df, label_col, column_name)
    if row is None:
        return {}
    start_n = _normalize_date(start)
    end_n = _normalize_date(end)
    out = {}
    for col in period_cols:
        d = _period_to_date(col)
        if d is None:
            continue
        if start_n and d < start_n:
            continue
        if end_n and d > end_n:
            continue
        out[d] = row[col]
    return out


def _dict_extract_value(result, column_name) -> Any:
    """Dict result (credit/analysis): column_name → top-level or nested key."""
    if not isinstance(result, dict):
        return None
    if column_name in result:
        return result[column_name]
    axes = result.get("axes")
    if isinstance(axes, dict) and column_name in axes:
        return axes[column_name]
    if isinstance(axes, list):
        for axis in axes:
            if isinstance(axis, dict) and column_name in axis:
                return axis[column_name]
    return None


# ---------------------------------------------------------------------------
# Extraction mixins (self-contained; each delegates to the module helpers)
# ---------------------------------------------------------------------------

class _WidePanelExtraction:
    """Wide-panel (항목 × period) extraction."""

    def extract_value(self, result, column_name, date, identifier=None):
        return _wide_extract_value(result, column_name, date)

    def extract_series(self, result, column_name, start, end, identifier=None):
        return _wide_extract_series(result, column_name, start, end)

    def build_range_params(self, fn, identifier, start, end, binding):
        return {"code": identifier}


class _LongFrameExtraction:
    """Long-frame (row-per-observation) extraction, mirroring the edinet adapter."""

    _ALIASES: dict[str, str] = {}

    def extract_value(self, result, column_name, date, identifier=None):
        return _long_extract_value(result, column_name, date, self._ALIASES)

    def extract_series(self, result, column_name, start, end, identifier=None):
        return _long_extract_series(result, column_name, start, end, self._ALIASES)

    def build_range_params(self, fn, identifier, start, end, binding):
        return {
            "code": identifier,
            "start": _normalize_date(start),
            "end": _normalize_date(end),
        }


# ---------------------------------------------------------------------------
# Base call() with retry loop
# ---------------------------------------------------------------------------

class _DartlabBase:
    """Shared retry/call scaffolding; subclasses implement ``_fetch``.

    ``_NEEDS_CODE`` gates the code-presence check (search is keyless);
    ``_NEEDS_DART_KEY`` gates the ``DART_API_KEY`` presence check (news/search
    are keyless public endpoints).
    """

    _NEEDS_CODE = True
    _NEEDS_DART_KEY = True
    _RETRIES = DEFAULT_RETRIES
    _RETRY_DELAY = DEFAULT_RETRY_DELAY

    def _fetch(self, dartlab, params) -> Any:  # pragma: no cover - abstract
        raise NotImplementedError

    def call(self, command: str, params: dict) -> Any:
        import time

        import dartlab  # lazy; requires the `data` extra + Python 3.12

        from fd_open_data_mcp.fetch.runner import FetchError, _ensure_dart_api_key

        if self._NEEDS_CODE and not params.get("code"):
            raise FetchError(f"company_* command {command} needs a code")
        if self._NEEDS_DART_KEY:
            _ensure_dart_api_key()

        last_exc: Exception | None = None
        for attempt in range(self._RETRIES + 1):
            try:
                return self._fetch(dartlab, params)
            except Exception as exc:  # noqa: BLE001 - upstream errors surface as FetchError
                last_exc = exc
                if attempt < self._RETRIES:
                    logger.debug(
                        "dartlab %s attempt %d/%d failed (%s); retrying in %.1fs",
                        command, attempt + 2, self._RETRIES + 1, exc, self._RETRY_DELAY,
                    )
                    time.sleep(self._RETRY_DELAY)
        raise FetchError(
            f"dartlab {command} failed after {self._RETRIES + 1} attempts: {last_exc}"
        ) from last_exc


# ---------------------------------------------------------------------------
# Command adapters
# ---------------------------------------------------------------------------

class CompanyPanelAdapter(_DartlabBase, _WidePanelExtraction):
    """``company_panel`` <- ``dartlab.Company(code).panel(key=None, freq=...)``."""

    def build_params(self, fn, identifier, date, binding):
        return {"code": identifier}

    def _fetch(self, dartlab, params):
        code = params["code"]
        company = dartlab.Company(code)
        kwargs = {
            k: v for k, v in (("key", params.get("key")), ("freq", params.get("freq")))
            if v is not None
        }
        # ``company.panel`` is a property returning a Panel (pl.DataFrame subclass);
        # calling it returns the wide grid.
        return company.panel(**kwargs)


class CompanyCreditAdapter(_DartlabBase):
    """``company_credit`` <- ``dartlab.Company(code).credit(axis, detail=...)`` → dict."""

    def build_params(self, fn, identifier, date, binding):
        axis = getattr(getattr(binding, "column", None), "name", None)
        return {"code": identifier, "axis": axis}

    def extract_value(self, result, column_name, date, identifier=None):
        return _dict_extract_value(result, column_name)

    def extract_series(self, result, column_name, start, end, identifier=None):
        return {}  # credit is a point-in-time rating, not a timeseries

    def _fetch(self, dartlab, params):
        code = params["code"]
        company = dartlab.Company(code)
        axis = params.get("axis")
        detail = params.get("detail")
        kw = {"detail": detail} if detail is not None else {}
        if axis is not None:
            return company.credit(axis, **kw)
        return company.credit(**kw)


class CompanyAnalysisAdapter(_DartlabBase):
    """``company_analysis`` <- ``dartlab.Company(code).analysis(axis, subaxis)``.

    Returns a dict (guide / axis breakdown) or a pl.DataFrame (metric grid);
    extraction tries the dict path first, then long-frame.
    """

    _ALIASES: dict[str, str] = {}

    def build_params(self, fn, identifier, date, binding):
        return {"code": identifier, "axis": binding.column.name}

    def extract_value(self, result, column_name, date, identifier=None):
        if isinstance(result, dict):
            return _dict_extract_value(result, column_name)
        return _long_extract_value(result, column_name, date, self._ALIASES)

    def extract_series(self, result, column_name, start, end, identifier=None):
        if isinstance(result, dict):
            return {}
        return _long_extract_series(result, column_name, start, end, self._ALIASES)

    def _fetch(self, dartlab, params):
        code = params["code"]
        company = dartlab.Company(code)
        axis = params.get("axis")
        subaxis = params.get("subaxis")
        if axis is not None and subaxis is not None:
            return company.analysis(axis, subaxis)
        if axis is not None:
            return company.analysis(axis)
        return company.analysis()


class CompanyNewsAdapter(_DartlabBase, _LongFrameExtraction):
    """``company_news`` <- ``dartlab.Company(code).news(days=...)`` (keyless RSS)."""

    _NEEDS_DART_KEY = False
    _ALIASES = {}  # columns: title, date, source, link

    def build_params(self, fn, identifier, date, binding):
        return {"code": identifier}

    def _fetch(self, dartlab, params):
        code = params["code"]
        company = dartlab.Company(code)
        days = params.get("days")
        return company.news(**({"days": days} if days is not None else {}))


class CompanyDisclosureAdapter(_DartlabBase, _LongFrameExtraction):
    """``company_disclosure`` <- ``dartlab.Company(code).disclosure(start, end, ...)``."""

    _ALIASES = {
        "filing_date": "filedAt", "doc_id": "docId", "form": "formType",
    }

    def build_params(self, fn, identifier, date, binding):
        d = _normalize_date(date)
        return {"code": identifier, "start": d, "end": d}

    def build_range_params(self, fn, identifier, start, end, binding):
        return {
            "code": identifier,
            "start": _normalize_date(start),
            "end": _normalize_date(end),
        }

    def _fetch(self, dartlab, params):
        code = params["code"]
        company = dartlab.Company(code)
        start = params.get("start")
        end = params.get("end")
        kwargs = {
            k: v for k, v in (
                ("type", params.get("type")),
                ("keyword", params.get("keyword")),
                ("finalOnly", params.get("finalOnly")),
            ) if v is not None
        }
        return company.disclosure(start, end, **kwargs)


class CompanySearchAdapter(_DartlabBase, _LongFrameExtraction):
    """``company_search`` <- ``dartlab.Company.search(keyword=..., limit=...)``.

    Keyless (KIND listing); no code, no DART key.
    """

    _NEEDS_CODE = False
    _NEEDS_DART_KEY = False
    _ALIASES = {"code": "stockCode", "name": "corpName"}

    def build_params(self, fn, identifier, date, binding):
        return {"keyword": identifier}

    def _fetch(self, dartlab, params):
        keyword = params.get("keyword")
        limit = params.get("limit")
        # ``search`` is a staticmethod on Company.
        return dartlab.Company.search(
            keyword, **({"limit": limit} if limit is not None else {})
        )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_all() -> None:
    register("dartlab", "company_panel", CompanyPanelAdapter())
    register("dartlab", "company_credit", CompanyCreditAdapter())
    register("dartlab", "company_analysis", CompanyAnalysisAdapter())
    register("dartlab", "company_news", CompanyNewsAdapter())
    register("dartlab", "company_disclosure", CompanyDisclosureAdapter())
    register("dartlab", "company_search", CompanySearchAdapter())


register_all()
