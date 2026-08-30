"""Read cn-report's extraction rules (llm_rules + script_rules) on demand.

Rules live in fd-cn-report's SQLite DB (default ``finddata/daas.db``,
overridable via ``CNREPORT_DATABASE_URL``). Read-only; not duplicated into the
ontology DB. Each rule maps a Chinese financial indicator to a
``document_type`` + ``module`` + extraction directive (``instruction`` for llm
rules, ``extract_rule`` for script rules).

Schema-tolerant: the rules DB evolves independently of this reader, so each
table's actual columns are probed with PRAGMA and only the wanted columns that
exist are selected. The current ``llm_rules`` schema drifted from the original:
``document_type`` became ``document_type_codes`` (a JSON list) and
``module``/``subgroup`` moved into the ``instruction`` text (a trailing
``source: {"statement": ...}`` JSON) — both are normalized back into the
reader's output shape, and filters that can't run in SQL fall back to Python.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Optional

from fd_open_data_mcp.catalog.providers import finddata_root

_LLM_COLS = (
    "indicator, document_type, module, subgroup, source_type, extractor, applies_to, "
    "unit, period_type, value_range, source, aliases, note, direction, instruction, position"
)
_SCRIPT_COLS = (
    "indicator, document_type, extract_rule, position, module, subgroup, source_type, "
    "applies_to, unit, period_type, source, aliases, note"
)

_SOURCE_JSON_RE = re.compile(r"source:\s*(\{.*\})\s*$", re.DOTALL)


def default_cnreport_db() -> str:
    return os.environ.get("CNREPORT_DATABASE_URL") or str(finddata_root() / "daas.db")


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _json_list(raw: Optional[str]) -> list[str]:
    """document_type_codes arrives as a JSON list string ('[\"cn_annual\", …]')."""
    if not raw:
        return []
    try:
        v = json.loads(raw)
    except (TypeError, ValueError):
        return [raw] if isinstance(raw, str) else []
    return v if isinstance(v, list) else [v]


def _module_from_instruction(instruction: str) -> Optional[str]:
    """llm_rules' module lives in the instruction's trailing source JSON:
    ``… | source: {"statement": "balance_sheet", "field": …}``."""
    if not instruction:
        return None
    m = _SOURCE_JSON_RE.search(instruction)
    if not m:
        return None
    try:
        src = json.loads(m.group(1))
    except (TypeError, ValueError):
        return None
    return src.get("statement") if isinstance(src, dict) else None


def read_cnreport_rules(
    db_path: Optional[str] = None,
    indicator: Optional[str] = None,
    document_type: Optional[str] = None,
    module: Optional[str] = None,
    kind: Optional[str] = None,
    limit: int = 100,
) -> tuple[list[dict], list[str]]:
    """Read llm_rules and/or script_rules. Returns (rules, errors)."""
    path = db_path or default_cnreport_db()
    if not Path(path).exists():
        return [], [f"rules DB not found: {path}"]

    out: list[dict] = []
    errors: list[str] = []
    kinds = ["llm", "script"] if kind is None else [kind]
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for k in kinds:
            table = "llm_rules" if k == "llm" else "script_rules"
            wanted = (_LLM_COLS if k == "llm" else _SCRIPT_COLS).split(", ")
            if table not in tables:
                errors.append(f"{table} not present in {path}")
                continue
            actual = _table_columns(conn, table)
            select = [c for c in wanted if c in actual]
            drifted = "document_type" not in actual and "document_type_codes" in actual
            if drifted:
                select.append("document_type_codes")

            sql = f"SELECT {', '.join(select)} FROM {table}"
            where, args, py_filters = [], [], []
            if indicator and "indicator" in actual:
                where.append("indicator = ?"); args.append(indicator)
            elif indicator:
                py_filters.append(("indicator", indicator))
            if document_type and "document_type" in actual:
                where.append("document_type = ?"); args.append(document_type)
            elif document_type:
                py_filters.append(("document_type", document_type))
            if module and "module" in actual:
                where.append("module = ?"); args.append(module)
            elif module:
                py_filters.append(("module", module))
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " LIMIT ?"; args.append(limit)

            for row in conn.execute(sql, args):
                d = dict(row)
                if drifted:
                    d["document_type"] = _json_list(d.pop("document_type_codes", None))
                d["kind"] = k
                if k == "llm":
                    d["extract"] = d.pop("instruction", "") or ""
                    if "module" not in d or d["module"] is None:
                        d["module"] = _module_from_instruction(d["extract"])
                else:
                    d["extract"] = d.pop("extract_rule", "") or ""
                # filters whose column doesn't exist in this table's schema
                skip = False
                for col, val in py_filters:
                    rv = d.get(col)
                    if isinstance(rv, list):
                        if val not in rv:
                            skip = True; break
                    elif rv != val:
                        skip = True; break
                if not skip:
                    out.append(d)
        conn.close()
    except Exception as e:  # noqa: BLE001
        errors.append(f"read_cnreport_rules error: {e}")
    return out, errors
