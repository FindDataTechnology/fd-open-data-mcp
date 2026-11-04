#!/usr/bin/env python3
"""Register crossborder-vertical entities into the entities DB (batches 3-5 of
docs/crossborder-entity-vocab-gap.md; batch 1 = sync_xb_regimes.py).

Grounding discipline (spec crossborder-vertical-data):
- platform: four well-known cross-border e-commerce platforms (factual catalog
  entities for the merchant-authorization connector channel, design D1).
- hs_code / currency_pair: ONLY entities backed by landed crawl data
  (un-comtrade cmd_code=854142; ecb-exr currency=CNY vs EUR reference).
- ports batch: DEFERRED — portwatch-imf landed rows are offshore oil terminals
  (FSO), not cross-border e-commerce ports; authoritative port-code list needs
  the customs channel (blocked, contract-layer pending human).
- policy batch: incremental by nature (registers as patrol output lands).

Usage:
    python scripts/sync_xb_entities.py --emit-sql > /tmp/xb_entities.sql
    # or apply directly with FD_OPEN_DATA_MCP_DATABASE_URL set
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BATCHES = {
    "platform": [
        # (code, name_zh, name_en, extra_meta)
        ("amazon", "亚马逊", "Amazon", {"marketplaces": ["US", "EU", "JP"]}),
        ("tiktok_shop", "TikTok Shop", "TikTok Shop",
         {"connector_first": True}),  # 11-03 ruling: first inner-ring connector
        ("temu", "Temu", "Temu", {}),
        ("shein", "SHEIN", "Shein", {}),
    ],
    "hs_code": [
        ("854142", "锂离子蓄电池", "Lithium-ion accumulators",
         {"hs_level": 6, "source": "un-comtrade landed cmd_code"}),
        ("851762", "电话及通信设备", "Phone/communication apparatus (HS 8517.62)",
         {"hs_level": 6, "source": "un-comtrade landed cmd_code"}),
        ("610910", "棉制针织T恤", "T-shirts, knitted, of cotton (HS 6109.10)",
         {"hs_level": 6, "source": "un-comtrade landed cmd_code"}),
    ],
    "currency_pair": [
        ("EURCNY", "欧元人民币", "EUR/CNY",
         {"base": "EUR", "quote": "CNY", "source": "ecb-exr landed currency=CNY"}),
    ],
    "port": [
        # 仅注册落库数据支撑的中国枢纽港（portwatch-imf 港口 id）；离岸油码头不注册
        ("port1188", "上海港", "Shanghai Port",
         {"source": "portwatch-imf", "portwatch_id": "port1188"}),
        ("port2027", "洋山港", "Shanghai Yangshan Port",
         {"source": "portwatch-imf", "portwatch_id": "port2027"}),
        ("port1189", "蛇口港（深圳）", "Shekou Port (Shenzhen)",
         {"source": "portwatch-imf", "portwatch_id": "port1189"}),
        ("port1069", "青岛港", "Qingdao Port",
         {"source": "portwatch-imf", "portwatch_id": "port1069"}),
    ],
}


def upsert_sql(batch: str) -> str:
    stmts = []
    for code, name_zh, name_en, extra in BATCHES[batch]:
        meta = {"category": "crossborder", "source": f"xb_{batch}_catalog"}
        meta.update(extra)
        m = json.dumps(meta, ensure_ascii=False)
        esc = lambda s: s.replace("'", "''")  # noqa: E731
        stmts.append(
            "INSERT INTO entities (entity_type, code, name_en, name_zh, metadata_json, updated_at)\n"
            f"VALUES ('{batch}', '{esc(code)}', '{esc(name_en)}', '{esc(name_zh)}', "
            f"'{esc(m)}'::jsonb, NOW())\n"
            "ON CONFLICT (entity_type, code) DO UPDATE SET\n"
            f"  name_en = COALESCE(NULLIF('{esc(name_en)}', ''), entities.name_en),\n"
            f"  name_zh = COALESCE(NULLIF('{esc(name_zh)}', ''), entities.name_zh),\n"
            f"  metadata_json = '{esc(m)}'::jsonb,\n"
            "  updated_at = NOW();"
        )
    stmts.append(
        f"SELECT 'xb_{batch}_now', count(*) FROM entities WHERE entity_type = '{batch}';"
    )
    return "\n".join(stmts) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--emit-sql", action="store_true")
    ap.add_argument("--batch", choices=list(BATCHES), help="single batch; default all")
    args = ap.parse_args(argv)
    batches = [args.batch] if args.batch else list(BATCHES)
    if args.emit_sql:
        for b in batches:
            sys.stdout.write(upsert_sql(b))
        return 0
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from fd_open_data_mcp import db as dbmod
    from sqlalchemy import text
    db = dbmod.get_database()
    with db.get_session() as session:
        for b in batches:
            for stmt in upsert_sql(b).split(";\n"):
                if stmt.strip() and not stmt.strip().startswith("SELECT"):
                    session.execute(text(stmt))
        session.commit()
    print(f"applied batches: {batches}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
