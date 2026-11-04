#!/usr/bin/env python3
"""Sync 跨境电商监管代码 entities into the entities DB (batch 1 of the
crossborder vertical registration plan, docs/crossborder-entity-vocab-gap.md).

Regime codes are logical-only entity types (entity-identity spec: entity_type
values not backed by a taxonomy table are allowed; precedents: organization,
person). Definitions below are taken verbatim from the in-production pilot
catalog skill (crossborder-regime-code-catalog) — never invented.

Usage:
    # apply directly (needs fd_open_data_mcp importable + DB env):
    FD_OPEN_DATA_MCP_DATABASE_URL=postgresql://... python scripts/sync_xb_regimes.py

    # or emit idempotent UPSERT SQL and apply it elsewhere (e.g. psql in pod):
    python scripts/sync_xb_regimes.py --emit-sql > /tmp/xb_regimes.sql

Idempotent: ON CONFLICT (entity_type, code) DO UPDATE.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ENTITY_TYPE = "customs_regime"

# (code, name_zh, name_en, definition_zh) — definitions verbatim from the
# pilot catalog skill; "原文未释" discipline applies: no speculative glosses.
REGIMES = [
    ("9610", "跨境电商零售出口", "Cross-border e-commerce retail export (B2C)",
     "跨境电商零售出口（B2C 直邮/集货）"),
    ("1210", "保税跨境电商", "Bonded cross-border e-commerce",
     "保税跨境电商（保税仓备货，含出口/进口分向）"),
    ("9710", "跨境电商 B2B 直接出口", "Cross-border e-commerce B2B direct export",
     "跨境电商 B2B 直接出口"),
    ("9810", "跨境电商出口海外仓", "Cross-border e-commerce overseas-warehouse export",
     "跨境电商出口海外仓"),
]


def upsert_sql() -> str:
    """Idempotent UPSERT statements for the regimes seed list."""
    stmts = []
    for code, name_zh, name_en, definition_zh in REGIMES:
        meta = json.dumps({
            "category": "crossborder_regime",
            "source": "customs_regime_catalog",
            "definition_zh": definition_zh,
        }, ensure_ascii=False)
        # single-quote escape for SQL literals
        esc = lambda s: s.replace("'", "''")  # noqa: E731
        stmts.append(
            "INSERT INTO entities (entity_type, code, name_en, name_zh, metadata_json, updated_at)\n"
            f"VALUES ('{ENTITY_TYPE}', '{esc(code)}', '{esc(name_en)}', '{esc(name_zh)}', "
            f"'{esc(meta)}'::jsonb, NOW())\n"
            "ON CONFLICT (entity_type, code) DO UPDATE SET\n"
            f"  name_en = COALESCE(NULLIF('{esc(name_en)}', ''), entities.name_en),\n"
            f"  name_zh = COALESCE(NULLIF('{esc(name_zh)}', ''), entities.name_zh),\n"
            f"  metadata_json = '{esc(meta)}'::jsonb,\n"
            "  updated_at = NOW();"
        )
    stmts.append(
        f"SELECT 'xb_regimes_now', count(*) FROM entities WHERE entity_type = '{ENTITY_TYPE}';"
    )
    return "\n".join(stmts) + "\n"


def apply_direct() -> int:
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from fd_open_data_mcp import db as dbmod
    from sqlalchemy import text

    db = dbmod.get_database()
    with db.get_session() as session:
        for stmt in upsert_sql().split(";\n"):
            if stmt.strip():
                session.execute(text(stmt))
        session.commit()
    print(f"applied {len(REGIMES)} regimes under entity_type={ENTITY_TYPE}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--emit-sql", action="store_true", help="print UPSERT SQL, do not connect")
    args = ap.parse_args(argv)
    if args.emit_sql:
        sys.stdout.write(upsert_sql())
        return 0
    return apply_direct()


if __name__ == "__main__":
    raise SystemExit(main())
