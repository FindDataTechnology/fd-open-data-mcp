"""edinet-tools (PyPI dist ``edinet-tools``) adapters.

edinet-tools imports as ``edinet_tools``; its API is object-oriented around
``Entity`` (like edgar's ``Company`` / yfinance's ``Ticker``):

  - ``edinet_tools.entity(code)`` resolves an entity by EDINET code ('E02144'),
    ticker ('7203' / '7203.T'), corporate number, or name. KEYLESS - it reads
    the bundled FSA registry CSVs.
  - ``entity.documents(doc_type="120", days=365)`` returns ``list[Document]``
    (NOT a DataFrame). This is the only path that requires ``EDINET_API_KEY``.

``Document`` objects carry the filing metadata used as extraction columns:
``doc_id``, ``doc_type_code``, ``doc_type_name``, ``filer_edinet_code``,
``filer_name``, ``filing_datetime`` (datetime), ``securities_code``,
``period_start``, ``period_end``, ``doc_description``. The date axis is
``filing_datetime``; ``extract_value`` coerces the list to a DataFrame and
matches on it, mirroring the edgar adapter (whose ``CompanyFilings`` is also
not a plain DataFrame).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fd_open_data_mcp.adapters import register

logger = logging.getLogger(__name__)

DEFAULT_RETRIES = 2
DEFAULT_RETRY_DELAY = 1.0
DEFAULT_LOOKBACK_DAYS = 365

# ``entity.documents(days=N)`` looks back N days from today (JST); when a target
# date is given we compute the lookback to cover it and add a small margin for
# weekends/holidays (EDINET only files on JST business days).
_LOOKBACK_MARGIN_DAYS = 7

# Date-axis candidates, tolerant of a coerced frame whose datetime column has
# been renamed upstream (filing_datetime is the canonical Document property).
_DATE_COL_CANDIDATES = ("filing_datetime", "filing_date", "date")

# Document properties materialised as DataFrame columns by ``_to_dataframe``.
_DOC_COLUMNS = (
    "doc_id",
    "doc_type_code",
    "doc_type_name",
    "filer_edinet_code",
    "filer_name",
    "filing_datetime",
    "securities_code",
    "period_start",
    "period_end",
    "doc_description",
)


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


def _to_dataframe(result) -> Any:
    """Coerce ``list[Document]`` (or dicts / a single Document) to a DataFrame.

    Returns the input unchanged if it is already a DataFrame; ``None`` if the
    shape is unrecognised or empty.
    """
    import pandas as pd

    if isinstance(result, pd.DataFrame):
        return result
    docs = result
    if not isinstance(docs, (list, tuple)):
        docs = [docs]
    rows = []
    for doc in docs:
        if isinstance(doc, dict):
            rows.append(doc)
            continue
        rows.append({col: getattr(doc, col, None) for col in _DOC_COLUMNS})
    if not rows:
        return None
    return pd.DataFrame(rows)


class _EdinetBase:
    """Shared extraction logic for edinet document-list results."""

    _ALIASES: dict[str, str] = {}
    _RETRIES = DEFAULT_RETRIES
    _RETRY_DELAY = DEFAULT_RETRY_DELAY

    @staticmethod
    def _find_date_col(df) -> Optional[str]:
        for candidate in _DATE_COL_CANDIDATES:
            if candidate in df.columns:
                return candidate
        return None

    def extract_value(self, result, column_name, date, identifier: Optional[str] = None):
        df = _to_dataframe(result)
        if df is None or getattr(df, "empty", True):
            return None
        col = column_name if column_name in df.columns else self._ALIASES.get(column_name)
        if col is None or col not in df.columns:
            return None
        target = _normalize_date(date)
        date_col = self._find_date_col(df)
        if date_col is not None and target is not None:
            target_compact = target.replace("-", "")
            for _, row in df.iterrows():
                row_date = _normalize_date(row[date_col])
                if row_date is None:
                    continue
                if row_date == target or row_date.replace("-", "") == target_compact:
                    return row[col]
            return None
        # No date axis to match on: fall back to the first row.
        return df.iloc[0][col]

    def extract_series(self, result, column_name, start, end, identifier: Optional[str] = None):
        df = _to_dataframe(result)
        if df is None or getattr(df, "empty", True):
            return {}
        col = column_name if column_name in df.columns else self._ALIASES.get(column_name)
        if col is None or col not in df.columns:
            return {}
        date_col = self._find_date_col(df)
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

    def build_range_params(self, fn, identifier, start, end, binding):
        return {
            "code": identifier,
            "start": _normalize_date(start),
            "end": _normalize_date(end),
        }


class EntityDocumentsAdapter(_EdinetBase):
    """``entity_documents`` <- ``edinet_tools.entity(code).documents(days=N)``."""

    def build_params(self, fn, identifier, date, binding):
        return {"code": identifier, "date": _normalize_date(date)}

    def call(self, command: str, params: dict) -> Any:
        import datetime as _dt
        import time

        import edinet_tools  # lazy; requires the `data` extra

        from fd_open_data_mcp.fetch.runner import FetchError, _ensure_edinet_api_key

        code = params.get("code")
        if not code:
            raise FetchError(f"entity_* command {command} needs a code")
        _ensure_edinet_api_key()

        # Compute the lookback so the window covers the requested date(s).
        anchor = params.get("start") or params.get("date")
        days = DEFAULT_LOOKBACK_DAYS
        if anchor:
            try:
                target = _dt.date.fromisoformat(_normalize_date(anchor))
                days = max(1, (_dt.date.today() - target).days + _LOOKBACK_MARGIN_DAYS)
            except (TypeError, ValueError):
                days = DEFAULT_LOOKBACK_DAYS

        doc_type = params.get("doc_type")
        last_exc: Exception | None = None
        for attempt in range(self._RETRIES + 1):
            try:
                entity = edinet_tools.entity(code)
                kwargs: dict[str, Any] = {"days": days}
                if doc_type:
                    kwargs["doc_type"] = doc_type
                return entity.documents(**kwargs)
            except Exception as exc:  # noqa: BLE001 - upstream errors surface as FetchError
                last_exc = exc
                if attempt < self._RETRIES:
                    logger.debug(
                        "edinet entity.documents attempt %d/%d failed (%s); retrying in %.1fs",
                        attempt + 2, self._RETRIES + 1, exc, self._RETRY_DELAY,
                    )
                    time.sleep(self._RETRY_DELAY)
        raise FetchError(
            f"edinet {command} failed after {self._RETRIES + 1} attempts: {last_exc}"
        ) from last_exc


def register_all() -> None:
    register("edinet", "entity_documents", EntityDocumentsAdapter())


register_all()
