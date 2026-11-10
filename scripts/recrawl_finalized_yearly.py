#!/usr/bin/env python3
"""年度序列的补救性重取（make-observation-vintage-readable 任务 6.1–6.3）。

背景
----
旧实现里年度观测「跨年即定型」（`obs_date[:4] < today[:4]`），而官方年度统计的
修订窗可以跨两三年。于是被过早定型的年度值**再也不会重抓**，库里的"最新值"其实
是"初值"——而且因为 `is_stale=False`，连告警都不会响。

定型窗口修正（`fetch/finalization.py`）之后，受影响年度重新处于可修订状态。本脚本
把这段时间里漏掉的修订补回来。

三条纪律
--------
1. **默认不写**。不带 `--apply` 只做盘点（`--dry` 语义），输出受影响概念与年份范围。
2. **必须声明理由**。重取会改写已定型值，写入路径的写闸要求 `revision_reason`；
   本脚本固定给一条可追溯的理由，落成 `observation_revisions` 留痕。
3. **限流**。逐概念、按批、可断点续跑；批间有间隔。不做全库并发重抓。

用法
----
    # 1) 盘点：只读，不改任何东西
    python scripts/recrawl_finalized_yearly.py --db-url "$DSN"

    # 2) 小范围试跑（一个概念）
    python scripts/recrawl_finalized_yearly.py --db-url "$DSN" --apply --limit 1

    # 3) 全量（分批 + 断点）
    python scripts/recrawl_finalized_yearly.py --db-url "$DSN" --apply \
        --batch-size 20 --sleep-s 5 --checkpoint /tmp/recrawl-yearly.json

判定「受影响概念」
------------------
同时满足：
  * 概念 `frequency` 属于年度类；
  * 存在观测行的日期落在「旧规则已定型、新规则仍未定型」的区间内——
    即 `旧切点 <= date < 新切点`。

这个集合就是"被过早定型、因此可能漏收修订"的部分。它**不是**"所有年度数据"：
更早的年份在两种规则下都已定型，重取它们没有意义（也不该动）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REVISION_REASON = (
    "定型窗口修正后补救性重取：旧规则年度观测跨年即定型，"
    "导致官方修订未入账（make-observation-vintage-readable 6.x）"
)


def _parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db-url", default=os.environ.get("FD_OPEN_DATA_MCP_DATABASE_URL"),
                   help="数据库 DSN（默认取 FD_OPEN_DATA_MCP_DATABASE_URL）")
    p.add_argument("--apply", action="store_true",
                   help="真的写库。不带此开关只盘点（只读）")
    p.add_argument("--limit", type=int, default=0,
                   help="最多处理多少个 (concept) 组合；0 = 不限")
    p.add_argument("--batch-size", type=int, default=20, help="每批概念数")
    p.add_argument("--sleep-s", type=float, default=5.0, help="批间间隔秒数")
    p.add_argument("--checkpoint", default=None, help="断点文件路径（JSON）")
    p.add_argument("--max-years", type=int, default=5,
                   help="单概念最多回溯多少年（防一次拉太长）")
    return p.parse_args(argv)


def find_affected(session, *, max_years: int = 5):
    """找出「被旧规则过早定型」的 (概念, 实体) 与其重取年份。

    旧规则：年度观测在**次年 1 月 1 日**即定型。
    新规则：在**次年末**定型（次年整年可修订）。

    因此受影响的**只有 prev_year 这一年**（今天 2026-11-10 → 2025）：
    旧规则在 2026-01-01 已把 2025 判为定型，新规则要到 2026-12-31 才定型。

    更早的年份在两种规则下都已定型，重取它们既无意义也不该动——把 ranges 设成
    「整段历史」会让 --apply 对每一对重抓五年，把上游压垮（实测：整段是 13,990 对
    / 62,470 行，而真正受影响的是 ~8,700 行，差 7 倍）。
    `max_years` 仅用于回看窗口上限，不参与重取范围。
    """
    from sqlalchemy import text

    today = datetime.now(timezone.utc).date()
    prev_year = today.year - 1
    rows = session.execute(text(
        """
        SELECT c.id AS concept_id, c.code, c.frequency,
               o.entity_type, o.entity_id,
               MIN(o.date) AS min_date, MAX(o.date) AS max_date, COUNT(*) AS n
        FROM semantic_observations o
        JOIN concepts c ON c.id = o.concept_id
        WHERE LOWER(COALESCE(c.frequency, '')) IN ('yearly', 'annual', 'year')
          AND o.granularity = 'year'
          -- 只取受影响的那一年：旧规则已定型、新规则仍可修订
          AND o.date >= :lo AND o.date <= :hi
        GROUP BY c.id, c.code, c.frequency, o.entity_type, o.entity_id
        ORDER BY n DESC
        """
    ), {"lo": f"{prev_year}-01-01", "hi": f"{prev_year}-12-31"}).all()
    return [dict(r._mapping) for r in rows], prev_year


def main(argv=None) -> int:
    args = _parse_args(argv)
    if not args.db_url:
        print("缺少 --db-url（或设置 FD_OPEN_DATA_MCP_DATABASE_URL）", file=sys.stderr)
        return 2
    os.environ["FD_OPEN_DATA_MCP_DATABASE_URL"] = args.db_url

    from fd_open_data_mcp.db import Database
    from fd_open_data_mcp.fetch.dispatch import read_range

    db = Database(args.db_url)
    session = db.get_session()
    
    try:
        affected, prev_year = find_affected(session, max_years=args.max_years)
        n_concepts = len({r["concept_id"] for r in affected})
        print(f"受影响的 (概念, 实体) 组合：{len(affected)} 个，涉及概念 {n_concepts} 个")
        print(f"关键年份：{prev_year}（旧规则次年 1 月 1 日即定型，新规则次年末定型）")
        print()
        for row in affected[:50]:
            print(f"  concept {row['concept_id']:>7} {str(row['code'])[:36]:<36} "
                  f"{row['entity_type']}/{row['entity_id']:<6} "
                  f"{row['min_date']}..{row['max_date']} rows={row['n']}")
        if len(affected) > 50:
            print(f"  … 另有 {len(affected) - 50} 个")

        if not args.apply:
            print()
            print("只读盘点完成。未写入任何数据。加 --apply 才会真的重取。")
            return 0

        ckpt_path = Path(args.checkpoint) if args.checkpoint else None
        done: set[str] = set()
        if ckpt_path and ckpt_path.exists():
            done = set(json.loads(ckpt_path.read_text()).get("done", []))
            print(f"断点：跳过已完成的 {len(done)} 个 (概念, 实体) 组合")

        def key_of(r):
            return f"{r['concept_id']}/{r['entity_type']}/{r['entity_id']}"

        todo = [r for r in affected if key_of(r) not in done]
        if args.limit:
            todo = todo[: args.limit]

        processed, failed = 0, 0
        for i, row in enumerate(todo, 1):
            key = key_of(row)
            try:
                read_range(
                    session, [row["concept_id"]],
                    row["entity_type"], row["entity_id"],
                    row["min_date"], row["max_date"],
                    revision_reason=REVISION_REASON,
                    # 补救的意义就是"不等 TTL"：这些行是新鲜的（300 天没到），
                    # 正常读会直接命中缓存、永不回源——那样跑一万次也补不回修订。
                    force_refetch=True,
                )
                processed += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"  [fail] {key}: {type(exc).__name__}: {exc}")
            if ckpt_path:
                ckpt_path.write_text(json.dumps({"done": sorted(done | {key})}))
            if i % args.batch_size == 0:
                print(f"  进度 {i}/{len(todo)}（成功 {processed} / 失败 {failed}），"
                      f"暂停 {args.sleep_s}s")
                time.sleep(args.sleep_s)

        print()
        print(f"完成：处理 {processed} 个，失败 {failed} 个（共 {len(todo)} 个待处理）")
        print("复核：用 change 的 evidence/README.md 里的核对 SQL 比对该源修订行数变化。")
        return 0 if failed == 0 else 1
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
