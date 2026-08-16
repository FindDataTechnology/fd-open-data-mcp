"""World Bank (wbgapi) adapter: param mapping + value extraction.

``run_wbgapi`` needs ``indicator`` (a World Bank series code like
``NY.GDP.MKTP.CD``) + ``economy`` (an ISO-3 country code like ``CHN``). Without
this adapter, ``fetch_handler._build_params`` falls back to the legacy
``{"symbol": identifier, "date": date}`` which lacks both keys, so
``run_wbgapi`` raises ``FetchError("get_indicator_data needs indicator + economy")``
before any World Bank call is made.

The binding's column name holds the series code (the indicator); the per-source
entity identifier holds the economy code.
"""
from __future__ import annotations

from typing import Any, Optional

from fd_open_data_mcp.adapters import register
from fd_open_data_mcp.errors import FetchError


class WbgapiAdapter:
    """Adapter for the wbgapi ``get_indicator_data`` command."""

    def build_params(
        self, fn: Any, identifier: str, date: str, binding: Optional[Any] = None,
    ) -> dict:
        """identifier is the economy code (e.g. ``CHN``); the binding's column
        name is the World Bank series code (e.g. ``NY.GDP.MKTP.CD``)."""
        indicator = getattr(getattr(binding, "column", None), "name", None)
        if not indicator:
            raise FetchError("wbgapi binding has no column name (series code)",
                             source="wbgapi", command=fn.command)
        return {"indicator": indicator, "economy": identifier, "date": date}

    def extract_value(
        self, result: Any, column_name: str, date: str, identifier: Optional[str] = None,
    ) -> Optional[str]:
        """``run_wbgapi`` returns ``pd.DataFrame({indicator: [val]}, index=[date_key])``.
        ``column_name`` is the indicator (series code). Pull the single cell."""
        try:
            import pandas as pd
        except ImportError:
            return None
        if not isinstance(result, pd.DataFrame) or column_name not in result.columns:
            return None
        if result.empty:
            return None
        val = result[column_name].iloc[0]
        return None if pd.isna(val) else str(val)


adapter_instance = WbgapiAdapter()
register("wbgapi", "get_indicator_data", adapter_instance)


if __name__ == "__main__":
    # ponytail: one runnable self-check for the param/extraction logic.
    import pandas as pd

    class _Col(str):
        @property
        def name(self):
            return str(self)

    class _Binding:
        column = _Col("NY.GDP.MKTP.CD")

    fn = type("fn", (), {"command": "get_indicator_data"})()
    a = WbgapiAdapter()

    params = a.build_params(fn, "CHN", "2023", _Binding())
    assert params == {"indicator": "NY.GDP.MKTP.CD", "economy": "CHN", "date": "2023"}, params

    df = pd.DataFrame({"NY.GDP.MKTP.CD": [17.7e12]}, index=["2023"])
    v = a.extract_value(df, "NY.GDP.MKTP.CD", "2023", "CHN")
    assert v == str(17.7e12), v
    assert a.extract_value(df, "OTHER", "2023") is None
    assert a.extract_value(None, "NY.GDP.MKTP.CD", "2023") is None
    assert a.extract_value(pd.DataFrame({"NY.GDP.MKTP.CD": [float("nan")]}, index=["2023"]),
                           "NY.GDP.MKTP.CD", "2023") is None
    print("wbgapi adapter self-check OK")
