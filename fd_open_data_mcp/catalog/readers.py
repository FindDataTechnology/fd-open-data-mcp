"""Registry readers: turn each provider's registry shape into normalized records.

Normalized record:
  {command, category, description, source_url, parameters, columns,
   verified, scanner_mode}
where columns = [{name, type, description}] and
parameters = [{name, type, required, description}].

Each reader returns (records, errors).
"""
from __future__ import annotations

import asyncio
import importlib
import importlib.util
import sqlite3
import sys
from pathlib import Path

from fd_open_data_mcp.catalog.providers import PROVIDERS


def _norm_column(raw: dict) -> dict:
    return {
        "name": raw.get("name") or raw.get("column_name"),
        "type": raw.get("type") or raw.get("column_type"),
        "description": raw.get("description") or raw.get("column_description"),
        "semantic_type": raw.get("semantic_type"),
    }


def _norm_record(raw: dict, command: str | None, scanner_mode: str) -> dict | None:
    cmd = command or raw.get("command") or raw.get("name")
    if not cmd:
        return None
    cols = raw.get("columns") or raw.get("function_columns") or []
    norm_cols = [_norm_column(c) for c in cols]
    norm_cols = [c for c in norm_cols if c["name"]]
    return {
        "command": cmd,
        "category": raw.get("category"),
        "description": raw.get("description"),
        "source_url": raw.get("source") or raw.get("source_url"),
        "parameters": raw.get("parameters") or [],
        "columns": norm_cols,
        "verified": True,
        "scanner_mode": scanner_mode,
    }


def read_db(path: str, scanner_mode: str) -> tuple[list[dict], list[str]]:
    """Read a shipped registry.db with `functions` (+ optional `function_columns`)."""
    if not Path(path).exists():
        return [], [f"registry.db not found: {path}"]
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "functions" not in tables:
            return [], [f"registry.db has no 'functions' table: {path} (tables: {sorted(tables)})"]
        has_cols = "function_columns" in tables
        funcs = conn.execute("SELECT * FROM functions").fetchall()
        records: list[dict] = []
        for f in funcs:
            d = dict(f)
            if has_cols:
                rows = conn.execute(
                    "SELECT * FROM function_columns WHERE function_id = ?", (d["id"],)
                ).fetchall()
                d["columns"] = [dict(c) for c in rows]
            else:
                d["columns"] = []
            rec = _norm_record(d, d.get("command"), scanner_mode)
            if rec:
                records.append(rec)
        return records, []
    except Exception as e:  # noqa: BLE001
        return [], [f"read_db error for {path}: {e}"]
    finally:
        conn.close()


def read_dict(module_path: str, attr: str, scanner_mode: str) -> tuple[list[dict], list[str]]:
    """Import a module and read a REGISTRY dict {command: {fields}}."""
    try:
        mod = importlib.import_module(module_path)
    except Exception as e:  # noqa: BLE001
        return [], [f"cannot import {module_path}: {e}"]
    registry = getattr(mod, attr, None)
    if not isinstance(registry, dict):
        return [], [f"{module_path}.{attr} is not a dict"]
    records: list[dict] = []
    for command, fields in registry.items():
        rec = _norm_record(fields, command, scanner_mode)
        if rec:
            records.append(rec)
    return records, []


def read_dict_by_path(path: str, attr: str, scanner_mode: str) -> tuple[list[dict], list[str]]:
    """Load a ``.py`` file by path and read a REGISTRY-style dict attribute.

    Unlike ``read_dict`` (which imports the provider package), this loads the
    file in isolation via ``importlib``, so the provider package does not need
    to be installed (e.g. yfinance's ``seed.py``). The file is expected to be a
    checked-in artifact of a sibling finddata package (trusted).
    """
    if not Path(path).exists():
        return [], [f"registry file not found: {path}"]
    try:
        spec = importlib.util.spec_from_file_location(
            f"_fd_odm_dict_{Path(path).stem}", path,
        )
        if spec is None or spec.loader is None:
            return [], [f"cannot create module spec for {path}"]
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:  # noqa: BLE001
        return [], [f"failed to load {path}: {e}"]
    registry = getattr(mod, attr, None)
    if not isinstance(registry, dict):
        return [], [f"{path}::{attr} is not a dict"]
    records: list[dict] = []
    for command, fields in registry.items():
        rec = _norm_record(fields, command, scanner_mode)
        if rec:
            records.append(rec)
    return records, []


def read_callable(module_path: str, func_name: str, scanner_mode: str) -> tuple[list[dict], list[str]]:
    """Import a module and call list_functions() -> list[dict]."""
    try:
        mod = importlib.import_module(module_path)
    except Exception as e:  # noqa: BLE001
        return [], [f"cannot import {module_path}: {e}"]
    func = getattr(mod, func_name, None)
    if not callable(func):
        return [], [f"{module_path}.{func_name} is not callable"]
    try:
        items = func() or []
    except Exception as e:  # noqa: BLE001
        return [], [f"{module_path}.{func_name}() raised: {e}"]
    records: list[dict] = []
    for raw in items:
        rec = _norm_record(raw, raw.get("name") or raw.get("command"), scanner_mode)
        if rec:
            records.append(rec)
    return records, []


def read_fd_world_adapter(
    adapter_sources: list[str], scanner_mode: str
) -> tuple[list[dict], list[str]]:
    """Read curated functions from fd-world source adapters (ckan, cnstats, ...).

    Calls each adapter's ``discover()`` directly, returning the static curated
    function metadata. This does NOT require the upstream package
    (akshare/ckanapi/wbgapi) to be installed - the curated lists live in code
    (e.g. ``CKAN_FUNCTIONS``/``CNSTATS_FUNCTIONS``). Fetch-time still needs the
    upstream package; registry/metadata ingestion does not.
    """
    try:
        from fd_world.sources.config import get_adapter
    except Exception as e:  # noqa: BLE001
        return [], [f"cannot import fd_world.sources.config (is fd-world installed?): {e}"]
    records: list[dict] = []
    errors: list[str] = []
    for source_name in adapter_sources:
        try:
            adapter = get_adapter(source_name)
            if adapter is None:
                errors.append(f"fd-world has no adapter for '{source_name}'")
                continue
            items = adapter.discover() or []
        except Exception as e:  # noqa: BLE001
            errors.append(f"fd-world adapter '{source_name}'.discover() raised: {e}")
            continue
        for raw in items:
            rec = _norm_record(raw, raw.get("name") or raw.get("command"), scanner_mode)
            if rec:
                records.append(rec)
    return records, errors


def read_mcp(server_cwd: str, scanner_mode: str) -> tuple[list[dict], list[str]]:
    """Best-effort FastMCP tool introspection. Requires the provider installed + importable."""
    cwd = Path(server_cwd)
    if not cwd.exists():
        return [], [f"server cwd not found: {server_cwd}"]
    sys.path.insert(0, str(cwd))
    try:
        server = importlib.import_module("server")
        mcp = getattr(server, "mcp", None)
        if mcp is None:
            for v in vars(server).values():
                if type(v).__name__ == "FastMCP":
                    mcp = v
                    break
        if mcp is None:
            return [], [f"no FastMCP instance found in {server_cwd}/server.py"]
        tools = asyncio.run(mcp.list_tools())
        records: list[dict] = []
        for t in tools:
            records.append({
                "command": getattr(t, "name", str(t)),
                "category": "mcp-tool",
                "description": getattr(t, "description", None),
                "source_url": None,
                "parameters": [],
                "columns": [],
                "verified": True,
                "scanner_mode": scanner_mode,
            })
        return records, []
    except Exception as e:  # noqa: BLE001
        return [], [f"mcp introspection failed for {server_cwd}: {e}"]
    finally:
        try:
            sys.path.remove(str(cwd))
        except ValueError:
            pass


def read_manifest(path: str, scanner_mode: str) -> tuple[list[dict], list[str]]:
    """Read a cn-gov-style manifest DB: `sources` + `datasource_columns`.

    Each source (a gov-archive crawler target) becomes one function record;
    its datasource_columns become the output columns. The manifest's
    `semantic_type` (title/date/url/category/...) is preserved as a
    column-level concept hint.
    """
    if not Path(path).exists():
        return [], [f"registry.db not found: {path}"]
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "sources" not in tables or "datasource_columns" not in tables:
            return [], [f"manifest db missing sources/datasource_columns: {path}"]
        srcs = conn.execute("SELECT * FROM sources").fetchall()
        records: list[dict] = []
        for src in srcs:
            d = dict(src)
            cols = conn.execute(
                "SELECT * FROM datasource_columns WHERE datasource_id = ?", (d["id"],)
            ).fetchall()
            columns = [{
                "name": c["column_name"],
                "type": c["column_type"],
                "description": c["description"] or None,
                "semantic_type": c["semantic_type"] or None,
            } for c in cols]
            records.append({
                "command": d["name"],
                "category": d.get("category_label") or d.get("category"),
                "description": d.get("description"),
                "source_url": d.get("url"),
                "parameters": [],
                "columns": columns,
                "verified": True,
                "scanner_mode": scanner_mode,
            })
        return records, []
    except Exception as e:  # noqa: BLE001
        return [], [f"read_manifest error for {path}: {e}"]
    finally:
        conn.close()


def read_provider(provider_name: str) -> tuple[list[dict], list[str]]:
    """Dispatch to the right reader based on the provider config."""
    cfg = PROVIDERS[provider_name]
    mode = cfg["scanner_mode"]
    kind = cfg["reader"]
    if kind == "db":
        return read_db(cfg["registry_db"](), mode)
    if kind == "manifest":
        return read_manifest(cfg["registry_db"](), mode)
    if kind == "dict":
        return read_dict(cfg["dict_module"], cfg["dict_attr"], mode)
    if kind == "dict_path":
        return read_dict_by_path(cfg["dict_path"](), cfg["dict_attr"], mode)
    if kind == "callable":
        return read_callable(cfg["callable_module"], cfg["callable_func"], mode)
    if kind == "fd_world_adapter":
        return read_fd_world_adapter(cfg["adapter_sources"], mode)
    if kind == "mcp":
        return read_mcp(cfg["server_cwd"](), mode)
    return [], [f"unknown reader kind '{kind}' for {provider_name}"]
