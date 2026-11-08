#!/usr/bin/env python
"""给 cn-report `extract_indicators` 建银行监管指标列并确认概念绑定（6.1 映射首建 / 6.3 路由前提）。

**为什么要建列**：`extract_indicators` 是长表工具（物理列只有 indicator/value/period），
而本体登记层的绑定模型是「一列一个概念」（参照 wbgapi 的 1500 列 / 1506 绑定、
akshare 财务指标的 86 列 / 25 绑定）。长表工具不建指标列就永远拿不到路由，
`plan_crawl` 会以 `no confirmed binding / no candidate source` 拒掉每个银行概念。

**做法**：按 `fd-cn-report/docs/bank-indicator-concepts.json` 的 17 项，
  · 缺则插入 `columns(function_id=752, name=<中文指标>, meaning=<英文名>, type='float')`
  · 缺则插入 `concept_bindings(concept_id, column_id, confidence=0.95,
    provenance='manual', reviewed=True)` —— manual 是**人工核过**（4.2 的 golden 锚
    逐值核过 85 次），不是机器猜的，也不是样本确认
幂等：同 (function_id, name) 的列与同 (concept_id, column_id) 的绑定都不重复插。

用法：
  python3 scripts/seed_bank_indicator_columns.py --dry-run
  python3 scripts/seed_bank_indicator_columns.py
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

MAPPING = _REPO.parent / "fd-cn-report" / "docs" / "bank-indicator-concepts.json"
FUNCTION_COMMAND = "extract_indicators"
CONFIDENCE = 0.95


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mapping", default=str(MAPPING))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    rows = json.loads(Path(args.mapping).read_text(encoding="utf-8"))["indicator_concepts"]
    url = os.environ.get("FD_OPEN_DATA_MCP_DATABASE_URL")
    if not url:
        raise SystemExit("未设置 FD_OPEN_DATA_MCP_DATABASE_URL")

    from fd_open_data_mcp.db import Database
    from fd_open_data_mcp.models import Concept, ConceptBinding, Function, FunctionColumn

    session = Database(url).get_session()
    added_cols = added_binds = existing = 0
    try:
        fn = session.query(Function).filter(Function.command == FUNCTION_COMMAND).first()
        if fn is None:
            raise SystemExit(f"函数不存在：{FUNCTION_COMMAND}")
        print(f"function id={fn.id} command={fn.command} verified={fn.verified}")

        for row in rows:
            concept = session.query(Concept).filter(
                Concept.code == row["code"], Concept.entity_type == "stock").first()
            if concept is None:
                raise SystemExit(f"概念不存在：{row['code']}（先跑 seed_bank_concepts.py）")

            col = session.query(FunctionColumn).filter_by(
                function_id=fn.id, name=row["indicator_zh"]).first()
            if col is None:
                print(f"  [col ] {row['indicator_zh']}")
                if not args.dry_run:
                    col = FunctionColumn(
                        function_id=fn.id, name=row["indicator_zh"], type="float",
                        meaning=row["name_en"], description=f"cn-report indicator: {row['name_en']}",
                    )
                    session.add(col)
                    session.flush()
                added_cols += 1
            if col is None:  # dry-run：无法确认绑定是否已存在，按「将新增」计
                added_binds += 1
                continue

            bind = session.query(ConceptBinding).filter_by(
                concept_id=concept.id, column_id=col.id).first()
            if bind is not None:
                existing += 1
                continue
            print(f"  [bind] {row['indicator_zh']} -> {row['code']}")
            if not args.dry_run:
                session.add(ConceptBinding(
                    concept_id=concept.id, column_id=col.id,
                    confidence=CONFIDENCE, provenance="manual", reviewed=True,
                ))
            added_binds += 1

        if not args.dry_run:
            session.commit()
        total = session.query(ConceptBinding).join(
            FunctionColumn, FunctionColumn.id == ConceptBinding.column_id
        ).filter(FunctionColumn.function_id == fn.id).count()
    finally:
        session.close()

    print(f"\ncols_added={added_cols} binds_added={added_binds} existed={existing} "
          f"dry_run={args.dry_run} function_bindings_total={total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
