#!/usr/bin/env python
"""落银行监管指标概念（awaken-banking-report-ingest 6.1）。

读 cn-report 侧的真源映射 `fd-cn-report/docs/bank-indicator-concepts.json`
（17 项监管指标的中文科目 → `financials.*`），把缺失的概念写进本体库
（`concepts` 表）。**只增不删、幂等**：已存在的 concept 不动，重复跑 inserted=0。

命名门（spec「抽取结果 SHALL 登记进语义观测层并过命名门」）：
  semantic_code(code) + name_zh + name_en + unit + frequency 齐备才写；
  缺任一项直接拒，绝不写半条——半条概念会让读取面看见无名指标。
写入的 concept 一律 `verified=True`（这批是人工核过的固定清单，不是机器猜的）。

用法（需要 FD_OPEN_DATA_MCP_DATABASE_URL 指向本体库）：
  python3 scripts/seed_bank_concepts.py --dry-run
  python3 scripts/seed_bank_concepts.py
  python3 scripts/seed_bank_concepts.py --mapping /path/to/bank-indicator-concepts.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

DEFAULT_MAPPING = _REPO.parent / "fd-cn-report" / "docs" / "bank-indicator-concepts.json"
REQUIRED = ("code", "name_zh", "name_en", "unit", "frequency")


def _load_mapping(path: Path) -> tuple[dict, list[dict]]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    rows = doc.get("indicator_concepts") or []
    bad = [r for r in rows if any(not r.get(k) for k in REQUIRED)]
    if bad:
        raise SystemExit(f"命名门不过（缺 {'/'.join(REQUIRED)}）：{[r.get('indicator_zh') for r in bad]}")
    return doc, rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mapping", default=str(DEFAULT_MAPPING))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    mapping_path = Path(args.mapping)
    if not mapping_path.exists():
        raise SystemExit(f"映射文件不存在：{mapping_path}")
    doc, rows = _load_mapping(mapping_path)

    url = os.environ.get("FD_OPEN_DATA_MCP_DATABASE_URL")
    if not url:
        raise SystemExit("未设置 FD_OPEN_DATA_MCP_DATABASE_URL（本体库 Postgres）")

    from fd_open_data_mcp.db import Database
    from fd_open_data_mcp.models import Concept

    db = Database(url)
    session = db.get_session()
    inserted = existed = 0
    try:
        for row in rows:
            # 按 (code, entity_type) 找，不把 frequency 纳入查询：concept 的唯一键含
            # frequency，若按含 frequency 的键查，改了口径（如 quarterly→yearly）会插出
            # 第二条同名概念，读取面就会看到两个同名指标。命中即原地改口径。
            q = session.query(Concept).filter(
                Concept.code == row["code"],
                Concept.entity_type == doc.get("entity_type", "stock"),
            )
            hit = q.first()
            if hit is not None:
                if hit.frequency != row["frequency"]:
                    print(f"  [upd ] {row['code']} id={hit.id} frequency "
                          f"{hit.frequency} -> {row['frequency']}")
                    if not args.dry_run:
                        hit.frequency = row["frequency"]
                    inserted += 0
                    existed += 1
                    continue
                existed += 1
                print(f"  [keep] {row['code']} id={hit.id} ({row['name_zh']})")
                continue
            print(f"  [add ] {row['code']} {row['name_zh']} {row['unit']} {row['frequency']}")
            if not args.dry_run:
                session.add(Concept(
                    code=row["code"],
                    name_zh=row["name_zh"],
                    name_en=row["name_en"],
                    category=row.get("category"),
                    unit=row["unit"],
                    measure=row.get("measure", "") or "",
                    frequency=row["frequency"],
                    entity_type=doc.get("entity_type", "stock"),
                    source=doc.get("source", "cn-report"),
                    verified=True,
                ))
            inserted += 1
        if not args.dry_run:
            session.commit()
            total = session.query(Concept).filter(Concept.code.like("financials.%")).count()
        else:
            total = None
    finally:
        session.close()

    print(f"\ninserted={inserted} existed={existed} dry_run={args.dry_run} "
          f"financials_total={total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
