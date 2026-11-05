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

BATCHES: dict[str, list] = {
    # policy 批不在 BATCHES（事件驱动动态批，见 upsert_sql policy 分支）
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


# 在册国家成员映射（与 xb_policy_port_etl.sql 的 CASE 对齐；映射外成员如实不注册）
POLICY_MEMBERS = {
    "China": "CN", "United States": "US", "Japan": "JP",
    "Brazil": "BR", "Canada": "CA", "Australia": "AU", "India": "IN",
}

POLICY_QUERY = """
WITH dedup AS (
  SELECT DISTINCT ON (payload->>'member', payload->>'title', payload->>'date')
         payload->>'member' AS member, payload->>'notification_id' AS nid,
         payload->>'type' AS ntype, payload->>'title' AS title,
         payload->>'date' AS ndate, payload->>'url' AS url
  FROM crawl_items i JOIN crawl_runs r ON r.id = i.run_id
  WHERE r.source = 'wto-eping'
)
SELECT nid, member, ntype, title, ndate, url FROM dedup
WHERE nid IS NOT NULL AND member = ANY(:members)
ORDER BY nid
"""


def policy_rows(db) -> list[tuple]:
    """policy 批=事件驱动（2.4/3.1 裁决）：随 wto-eping 落库增量注册在册成员条目。"""
    from sqlalchemy import text

    with db.get_session() as session:
        rows = session.execute(text(POLICY_QUERY), {"members": list(POLICY_MEMBERS)}).fetchall()
    out = []
    for nid, member, ntype, title, ndate, url in rows:
        out.append((nid, title, "", {
            "source": "wto-eping", "member": member, "member_iso": POLICY_MEMBERS[member],
            "notification_type": ntype, "notification_date": str(ndate), "url": url,
        }))
    return out


def upsert_sql(batch: str) -> str:
    def one(code, name_zh, name_en, extra):
        meta = {"category": "crossborder", "source": f"xb_{batch}_catalog"}
        meta.update(extra)
        m = json.dumps(meta, ensure_ascii=False)
        esc = lambda s: s.replace("'", "''")  # noqa: E731
        return (
            "INSERT INTO entities (entity_type, code, name_en, name_zh, metadata_json, updated_at)\n"
            f"VALUES ('{batch}', '{esc(code)}', '{esc(name_en)}', '{esc(name_zh)}', "
            f"'{esc(m)}'::jsonb, NOW())\n"
            "ON CONFLICT (entity_type, code) DO UPDATE SET\n"
            f"  name_en = COALESCE(NULLIF('{esc(name_en)}', ''), entities.name_en),\n"
            f"  name_zh = COALESCE(NULLIF('{esc(name_zh)}', ''), entities.name_zh),\n"
            f"  metadata_json = '{esc(m)}'::jsonb,\n"
            "  updated_at = NOW();"
        )

    stmts = []
    if batch == "policy":
        # 动态批：SQL 形态需直连库取 wto-eping 快照
        from fd_open_data_mcp import db as dbmod

        for code, name_en, name_zh, extra in policy_rows(dbmod.get_database()):
            stmts.append(one(code, name_zh, name_en, extra))
    else:
        for code, name_zh, name_en, extra in BATCHES[batch]:
            stmts.append(one(code, name_zh, name_en, extra))
    stmts.append(
        f"SELECT 'xb_{batch}_now', count(*) FROM entities WHERE entity_type = '{batch}';"
    )
    return "\n".join(stmts) + "\n"


# 跨源标识映射（entity_source_identifiers，uq=(entity_type, entity_id, source)）：
# 只挂有落库数据支撑的源。dispatch 依赖它做 resolve_identifier（fetch/dispatch.py）。
IDENTIFIER_MAP = {
    # entity_type: (source, code→identifier 提取函数名)
    "port": ("portwatch-imf", None),          # code 即 portwatch id（port1188…）
    "hs_code": ("un-comtrade", None),         # code 即 cmd_code（854142…）
    "currency_pair": ("ecb-exr", None),       # code 即 EURCNY 形态
    "policy": ("wto-eping", None),            # code 即 notification_id
    "customs_regime": ("un-comtrade", None),  # partner code 走 ISO 数字码同型
}

# country 在册成员 → wto-eping member 名（2401 dispatch 解析用）
COUNTRY_IDENTIFIERS = {"CN": "China", "US": "United States", "JP": "Japan",
                       "BR": "Brazil", "CA": "Canada", "AU": "Australia", "IN": "India"}

IDENTIFIER_SQL = """
INSERT INTO entity_source_identifiers (entity_type, entity_id, source, identifier, created_at)
SELECT :etype, e.id, :src, :ident, NOW() FROM entities e
WHERE e.entity_type = :etype AND e.code = :code
ON CONFLICT (entity_type, entity_id, source) DO UPDATE
  SET identifier = EXCLUDED.identifier, created_at = NOW();
"""


def identifiers_sql(db) -> str:
    """按已注册实体生成跨源标识 UPSERT（事件驱动：只挂库里有行的实体）。"""
    from sqlalchemy import text

    out = []
    with db.get_session() as session:
        for etype, (src, _) in IDENTIFIER_MAP.items():
            rows = session.execute(
                text("SELECT code FROM entities WHERE entity_type = :t ORDER BY code"),
                {"t": etype}).fetchall()
            for (code,) in rows:
                out.append((etype, src, code, code))
        for iso, member in COUNTRY_IDENTIFIERS.items():
            out.append(("country", "wto-eping", iso, member))
    stmts = []
    for etype, src, code, ident in out:
        stmts.append(IDENTIFIER_SQL.replace(":etype", f"'{etype}'").replace(
            ":src", f"'{src}'").replace(":code", f"'{code}'").replace(":ident", f"'{ident}'"))
    stmts.append("SELECT 'xb_identifiers', count(*) FROM entity_source_identifiers i "
                 "JOIN entities e ON e.id = i.entity_id "
                 "WHERE e.metadata_json->>'category' = 'crossborder' "
                 "OR e.entity_type IN ('policy','port','platform','customs_regime','hs_code','currency_pair');")
    return "\n".join(stmts) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--emit-sql", action="store_true")
    ap.add_argument("--batch", choices=list(BATCHES) + ["policy"],
                    help="single batch; policy=事件驱动动态批（wto-eping 落库增量）; default all")
    ap.add_argument("--identifiers", action="store_true",
                    help="注册跨源标识（entity_source_identifiers，验收门槛 3）")
    args = ap.parse_args(argv)
    batches = [args.batch] if args.batch else list(BATCHES)
    if args.emit_sql:
        for b in batches:
            sys.stdout.write(upsert_sql(b))
        if args.identifiers:
            from fd_open_data_mcp import db as dbmod

            sys.stdout.write(identifiers_sql(dbmod.get_database()))
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
        if args.identifiers:
            for stmt in identifiers_sql(db).split(";\n"):
                if stmt.strip() and not stmt.strip().startswith("SELECT"):
                    session.execute(text(stmt))
        session.commit()
    print(f"applied batches: {batches} identifiers={args.identifiers}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
