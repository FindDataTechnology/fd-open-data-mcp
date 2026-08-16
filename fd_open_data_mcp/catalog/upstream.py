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


def introspect_edinet() -> list[dict]:
    """Flatten edinet_tools.Entity methods + top-level edinet_tools callables.

    edinet-tools (PyPI dist) imports as ``edinet_tools``; its surface is the
    Entity object (like edgar's Company). Requires the ``data`` extra; raises
    ImportError if absent (caught by the importer's ``_import_upstream_extras``).
    """
    import edinet_tools  # lazy; requires the `data` extra (edinet-tools dist)

    out: list[dict] = []
    entity_cls = getattr(edinet_tools, "Entity", None)
    if entity_cls is not None:
        for method_name in dir(entity_cls):
            if method_name.startswith("_"):
                continue
            attr = getattr(entity_cls, method_name)
            if not (inspect.isfunction(attr) or inspect.ismethod(attr)):
                continue
            out.append({
                "command": f"entity_{method_name}",
                "category": "entity",
                "description": _doc(attr),
                "source_url": None,
                "parameters": [],
                "columns": [],
            })
    for name, obj in vars(edinet_tools).items():
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


def introspect_dartlab() -> list[dict]:
    """Flatten dartlab.Company attributes + top-level dartlab callables.

    dartlab (PyPI dist ``dartlab``) imports as ``dartlab``; requires Python 3.12.
    ``Company`` is a factory routing via a provider ``canHandle`` chain; at the
    class level ``panel``/``credit``/``analysis``/``quant`` are ``property``
    descriptors (callable proxies) and ``news``/``disclosure``/``search`` are
    methods. Class-level ``None`` attrs (``show``/``collect``) are skipped.
    Requires the ``data`` extra + Python 3.12; raises ImportError if absent
    (caught by the importer's ``_import_upstream_extras``).
    """
    import dartlab  # lazy; requires the `data` extra + Python 3.12

    out: list[dict] = []
    company_cls = getattr(dartlab, "Company", None)
    if company_cls is not None:
        for method_name in dir(company_cls):
            if method_name.startswith("_"):
                continue
            attr = getattr(company_cls, method_name, None)
            if attr is None:
                # Skip class-level NoneType attrs (e.g. show/collect).
                continue
            # panel/credit/analysis/quant are property descriptors; news/
            # disclosure/search are functions/staticmethods.
            if not (
                isinstance(attr, property)
                or inspect.isfunction(attr)
                or inspect.ismethod(attr)
            ):
                continue
            out.append({
                "command": f"company_{method_name}",
                "category": "company",
                "description": _doc(attr),
                "source_url": None,
                "parameters": [],
                "columns": [],
            })
    for name, obj in vars(dartlab).items():
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


def introspect_ckan() -> list[dict]:
    """CKAN's surface is the action API (``RemoteCKAN(portal).action.<verb>``),
    not a flat module of callables, so a traditional introspector doesn't apply.
    The curated seed (``catalog/seeds/ckan.py``) is authoritative; return ``[]``
    so ``introspect_upstream("ckan")`` is a documented no-op rather than a
    silent fall-through.
    """
    return []


def introspect_cnstats() -> list[dict]:
    """cnstats is a curated 8-command mapping to akshare macro functions, not a
    flat module of callables, so a traditional introspector doesn't apply (the
    akshare macro functions themselves are already surfaced by
    ``introspect_akshare``). The curated seed (``catalog/seeds/cnstats.py``) is
    authoritative; return ``[]`` so ``introspect_upstream("cnstats")`` is a
    documented no-op rather than a silent fall-through.
    """
    return []


def introspect_upstream(upstream: str) -> list[dict]:
    """Dispatch to the right upstream introspector by package name."""
    if upstream == "akshare":
        return introspect_akshare()
    if upstream == "yfinance":
        return introspect_yfinance()
    if upstream == "edgar":
        return introspect_edgar()
    if upstream == "edinet":
        return introspect_edinet()
    if upstream == "dartlab":
        return introspect_dartlab()
    if upstream == "wbgapi":
        return introspect_wbgapi()
    if upstream == "ckan":
        return introspect_ckan()
    if upstream == "cnstats":
        return introspect_cnstats()
    return []
