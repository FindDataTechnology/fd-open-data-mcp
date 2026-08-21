"""wbgapi adapters: ``get_indicator_data`` (indicator/economy per-date fetch).

The scraw ``fetch_handler`` legacy fallback builds ``{symbol, date}`` params,
which ``run_wbgapi`` rejects ("needs indicator + economy"). Registering this
adapter gives the correct mapping: ``economy`` <- resolved entity id,
``indicator`` <- the binding's column name (the WDI series code, e.g.
``NY.GDP.PCAP.CD``), ``date`` <- the requested year. ``run_wbgapi`` already
reshapes the result into ``DataFrame({indicator: [val]}, index=[year])`` so
``extract_value`` is a plain column lookup.
"""
from __future__ import annotations

from typing import Any, Optional


class _GetIndicatorDataAdapter:
    def build_params(self, fn, identifier: str, date: str, binding) -> dict:
        column = binding.column.name if binding is not None else None
        year = (date or "")[:4]
        params: dict[str, Any] = {"economy": identifier}
        if column:
            params["indicator"] = column
        if year.isdigit():
            params["date"] = year
        return params

    def extract_value(self, result, column_name: str, date: str,
                      identifier: Optional[str] = None) -> Any:
        try:
            import pandas as pd
        except ImportError:
            return None
        if not isinstance(result, pd.DataFrame) or column_name not in result.columns:
            return None
        # ponytail: normalize index to year-strings once, then label-lookup.
        s = result[column_name]
        s.index = [str(i)[:4] for i in s.index]
        year = (date or "")[:4]
        if year in s.index:
            return s[year]
        return s.iloc[0] if len(s) == 1 else None  # single-cell reshaped by run_wbgapi

    def build_range_params(self, fn, identifier: str, start: str, end: str, binding):
        params = self.build_params(fn, identifier, start, binding)
        params["date"] = (start or "")[:4]
        return params

    def extract_series(self, result, column_name: str, start: str, end: str):
        try:
            import pandas as pd
        except ImportError:
            return {}
        if not isinstance(result, pd.DataFrame) or column_name not in result.columns:
            return {}
        out = {}
        for idx, val in zip(result.index, result[column_name].tolist()):
            d = str(idx)
            if len(d) == 4:  # annual series indexed by year
                d = f"{d}-12-31"
            if start <= d <= end and not pd.isna(val):
                out[d] = val
        return out


# Register at import (mirrors akshare.py side-effect registration). Imported
# last by ``adapters/__init__.py`` so ``register`` is defined before this runs.
from fd_open_data_mcp.adapters import register as _register  # noqa: E402

_register("wbgapi", "get_indicator_data", _GetIndicatorDataAdapter())
