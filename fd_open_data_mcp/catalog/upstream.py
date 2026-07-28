"""Upstream-package introspection.

Returns the full callable surface of an upstream library (akshare module
functions; yfinance Ticker methods + top-level callables). The importer
marks callables absent from the curated fd-* registry as ``verified=False``.
"""
from __future__ import annotations

import inspect
from typing import Any


def _doc(obj: Any) -> str | None:
    d = (obj.__doc__ or "").strip()
    return d or None


def introspect_akshare() -> list[dict]:
    """All public callables in the akshare module."""
    import akshare as ak  # lazy; requires the `data` extra

    out: list[dict] = []
    for name, obj in vars(ak).items():
        if name.startswith("_") or not inspect.isfunction(obj):
            continue
        out.append({
            "command": name,
            "category": None,
            "description": _doc(obj),
            "source_url": None,
            "parameters": [],
            "columns": [],
        })
    return out


def introspect_yfinance() -> list[dict]:
    """Flatten yfinance.Ticker methods into ticker_<method> commands + top-level callables.

    yfinance exposes only ~5 top-level module callables; its real surface is
    the Ticker object's methods (design.md D6). fd-yfinance/core/seed.py
    demonstrates this flattening convention.
    """
    import yfinance as yf  # lazy; requires the `data` extra

    out: list[dict] = []
    for method_name in dir(yf.Ticker):
        if method_name.startswith("_"):
            continue
        attr = getattr(yf.Ticker, method_name)
        if not (inspect.isfunction(attr) or inspect.ismethod(attr)):
            continue
        out.append({
            "command": f"ticker_{method_name}",
            "category": "ticker",
            "description": _doc(attr),
            "source_url": None,
            "parameters": [],
            "columns": [],
        })
    for name, obj in vars(yf).items():
        if name.startswith("_") or not inspect.isfunction(obj):
            continue
        out.append({
            "command": name,
            "category": "top-level",
            "description": _doc(obj),
            "source_url": None,
            "parameters": [],
            "columns": [],
        })
    return out


def introspect_edgar() -> list[dict]:
    """Flatten edgar.Company methods + top-level edgar callables.

    edgartools imports as ``edgar``; its surface is the Company object (like
    yfinance's Ticker). Requires the ``data`` extra; raises ImportError if
    absent (caught by the importer's ``_import_upstream_extras``).
    """
    import edgar  # lazy; requires the `data` extra (edgartools dist)

    out: list[dict] = []
    for method_name in dir(edgar.Company):
        if method_name.startswith("_"):
            continue
        attr = getattr(edgar.Company, method_name)
        if not (inspect.isfunction(attr) or inspect.ismethod(attr)):
            continue
        out.append({
            "command": f"company_{method_name}",
            "category": "company",
            "description": _doc(attr),
            "source_url": None,
            "parameters": [],
            "columns": [],
        })
    for name, obj in vars(edgar).items():
        if name.startswith("_") or not inspect.isfunction(obj):
            continue
        out.append({
            "command": name,
            "category": "top-level",
            "description": _doc(obj),
            "source_url": None,
            "parameters": [],
            "columns": [],
        })
    return out


def introspect_wbgapi() -> list[dict]:
    """Flatten wbgapi submodule callables (wb.data, wb.economy, wb.series, ...).

    wbgapi is organized as submodules; each callable becomes
    ``<submodule>_<callable>`` (unverified). Requires the ``data`` extra.
    """
    import wbgapi as wb  # lazy; requires the `data` extra

    out: list[dict] = []
    for sub_name in ("data", "economy", "series", "time", "region", "source", "topic", "indicator"):
        sub = getattr(wb, sub_name, None)
        if sub is None:
            continue
        for attr_name in dir(sub):
            if attr_name.startswith("_"):
                continue
            attr = getattr(sub, attr_name)
            if not (inspect.isfunction(attr) or inspect.ismethod(attr)):
                continue
            out.append({
                "command": f"{sub_name}_{attr_name}",
                "category": f"wbgapi.{sub_name}",
                "description": _doc(attr),
                "source_url": None,
                "parameters": [],
                "columns": [],
            })
    return out


def introspect_upstream(upstream: str) -> list[dict]:
    """Dispatch to the right upstream introspector by package name."""
    if upstream == "akshare":
        return introspect_akshare()
    if upstream == "yfinance":
        return introspect_yfinance()
    if upstream == "edgar":
        return introspect_edgar()
    if upstream == "wbgapi":
        return introspect_wbgapi()
    return []
