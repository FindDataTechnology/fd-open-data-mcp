"""Read cn-report's extraction rules (llm_rules + script_rules) on demand.

Rules live in fd-cn-report's SQLite DB (default ``finddata/daas.db``,
overridable via ``CNREPORT_DATABASE_URL``). Read-only; not duplicated into the
ontology DB. Each rule maps a Chinese financial indicator to a
``document_type`` + ``module`` + extraction directive (``instruction`` for llm
rules, ``extract_rule`` for script rules).
"""
from __future__ import annotations

import os
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


def default_cnreport_db() -> str:
    return os.environ.get("CNREPORT_DATABASE_URL") or str(finddata_root() / "daas.db")


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
            cols = _LLM_COLS if k == "llm" else _SCRIPT_COLS
            if table not in tables:
                errors.append(f"{table} not present in {path}")
                continue
            sql = f"SELECT {cols} FROM {table}"
            where, args = [], []
            if indicator:
                where.append("indicator = ?"); args.append(indicator)
            if document_type:
                where.append("document_type = ?"); args.append(document_type)
            if module:
                where.append("module = ?"); args.append(module)
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " LIMIT ?"; args.append(limit)
            for row in conn.execute(sql, args):
                d = dict(row)
                d["kind"] = k
                if k == "llm":
                    d["extract"] = d.pop("instruction", "") or ""
                else:
                    d["extract"] = d.pop("extract_rule", "") or ""
                out.append(d)
        conn.close()
    except Exception as e:  # noqa: BLE001
        errors.append(f"read_cnreport_rules error: {e}")
    return out, errors
