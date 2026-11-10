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

#: 同一年的新旧值相差超过这个倍数，就不是"修订"而是"换了个指标/单位"。
SCALE_TOLERANCE = 100.0


def _as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _year_of(date) -> str:
    return str(date)[:4]


def judge_series(series, before):
    """上游这批返回值能不能当「修订」写回？纯函数，便于单测。

    ``series``: 本次抓到的 ``{date: value}``；``before``: 库里现有的 year 粒度行。

    两道门，都是实测踩出来的（2026-11-10，concept 258 / stock 2859）：

    1. **粒度**：年频概念一年只该有一个点。上游若把季报/日报塞进来，
       写回会造出「同一年里四个年末日期、粒度标 year」的假历史
       （实测写入 2025-03-31/06-30/09-30/12-31 四个点）。
    2. **量级**：同一年的新旧值差两个数量级以上，说明那个位置放的是另一个指标
       （实测 67605246103.24 → 2.6719，把营收换成了每股指标）。

    这两件事都不是修订。放行它们不是"补回修订"，是把序列改坏。
    """
    if not series:
        return False, "empty_series"

    per_year: dict[str, int] = {}
    for d in series:
        y = _year_of(d)
        per_year[y] = per_year.get(y, 0) + 1
    dense = {y: n for y, n in per_year.items() if n > 1}
    if dense:
        y, n = sorted(dense.items())[0]
        return False, f"finer_granularity_returned：{y} 年返回 {n} 个点（年频概念应为 1 个）"

    before_year: dict[str, list[float]] = {}
    for d, v in (before or {}).items():
        f = _as_float(v)
        if f is not None:
            before_year.setdefault(_year_of(d), []).append(f)

    for d, new in series.items():
        olds = before_year.get(_year_of(d))
        nf = _as_float(new)
        if not olds or nf is None:
            continue
        old = olds[0]
        if not old:
            continue
        ratio = abs(nf / old)
        if ratio > SCALE_TOLERANCE or ratio < 1.0 / SCALE_TOLERANCE:
            # 报"大多少倍"永远取 >= 1 的那一侧：ratio<1 时直接打印会出现
            # "差 0 倍"（实测 6.6021e+07 → 4.7755 打成"差 0 倍"），读的人无从判断。
            factor = ratio if ratio >= 1 else 1.0 / ratio
            direction = "缩" if ratio < 1 else "放"
            return False, (
                f"scale_mismatch：{d} {old:g} → {nf:g}"
                f"（{direction}了 {factor:,.0f} 倍，疑似换了指标/单位）"
            )
    return True, None


def _year_rows(session, concept_id, entity_type, entity_id, year):
    """该对在目标年内的 year 粒度行 ``{date: value}``——写入前快照，用来判口径。

    用 ``read_cache_range`` 而不是手写 ``date LIKE 'YYYY%'``。原因不是风格：
    ``LIKE`` 用不上 btree 索引，规划器会把分区扫一遍，其中包括挂在**外部服务器**
    ``shard_xinru3`` 上的那份；那个 server 一不可达，整条 session 就废了。
    实测（2026-11-10 生产批次）：第一条报
    ``OperationalError: could not connect to server "shard_xinru3"``，
    其后每一条都变成 ``InFailedSqlTransaction``，40/40 全红——而真因只有一个。

    ``read_cache_range`` 走 ``sql_bounds`` 的整年区间（可索引的范围条件）再按
    ``observation_period`` 判期，所以 bare-year 的 ``'2025'`` 行同样在窗口内。
    """
    from fd_open_data_mcp.fetch.cache import read_cache_range

    rows = read_cache_range(
        session, concept_id, entity_type, entity_id, str(year), str(year),
    )
    return {
        r.date: r.value
        for r in rows
        if getattr(r, "granularity", None) == "year"
    }


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

    **排除 deprecated 概念**：写路径的 ``check_applicability`` 会直接抛
    ``ConceptDeprecated``，重取这类概念必然失败、只是一遍遍污染计数。实测生产
    2,584 个概念里 1,607 个已 deprecated——不排除的话，盘点表里一大半是永远补不动的
    配对，真正的可补救项会被淹没。
    """
    from sqlalchemy import text

    today = datetime.now(timezone.utc).date()
    prev_year = today.year - 1
    rows = session.execute(text(
        """
        SELECT c.id AS concept_id, c.code, c.frequency,
               o.entity_type, o.entity_id,
               COUNT(*) AS n
        FROM semantic_observations o
        JOIN concepts c ON c.id = o.concept_id
        WHERE LOWER(COALESCE(c.frequency, '')) IN ('yearly', 'annual', 'year')
          AND o.granularity = 'year'
          -- 用**前缀**而不是日期区间：库里同一概念混着 `2025`（bare year）与
          -- `2025-09-27` 两种形状，而字符串比较下 '2025' < '2025-01-01'，
          -- 用 >= '2025-01-01' 会把 bare-year 行静默漏掉（实测漏 53 行）。
          AND o.date LIKE :yprefix
          -- 废弃概念写不进去：写路径的 check_applicability 直接抛 ConceptDeprecated。
          -- 它们的观测行仍留在库里，但重取只会得到异常，不该占盘点名额。
          AND NOT c.deprecated
        GROUP BY c.id, c.code, c.frequency, o.entity_type, o.entity_id
        ORDER BY n DESC
        """
    ), {"yprefix": f"{prev_year}%"}).all()
    out = []
    for r in rows:
        d = dict(r._mapping)
        # 重取范围 = **整年**，不是"数据点的最小到最大"。
        # 年频概念的上游返回带的是年内日期/年末日期；若按单点窗口去要
        # （如 max=min=2025-09-27），`_extract_series` 会因为返回值超出窗口而
        # 丢掉全部点 —— 那样抓了也写不进任何东西（实测 rows 只回缓存）。
        d["min_date"] = f"{prev_year}-01-01"
        d["max_date"] = f"{prev_year}-12-31"
        out.append(d)
    return out, prev_year


def main(argv=None) -> int:
    args = _parse_args(argv)
    if not args.db_url:
        print("缺少 --db-url（或设置 FD_OPEN_DATA_MCP_DATABASE_URL）", file=sys.stderr)
        return 2
    os.environ["FD_OPEN_DATA_MCP_DATABASE_URL"] = args.db_url

    from fd_open_data_mcp.db import Database
    from fd_open_data_mcp.fetch.dispatch import _read_range_one_ex

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

        accepted = refused = unavailable = unchanged = failed = 0
        for i, row in enumerate(todo, 1):
            key = key_of(row)
            try:
                # 写入前快照：判断上游返回值能不能当修订。必须在写之前拿到，
                # 因为 write_cache_range 内部自己 commit，写完就撤不回来了。
                before = _year_rows(session, row["concept_id"],
                                    row["entity_type"], row["entity_id"], prev_year)

                def _admit(series, source, _before=before):
                    return judge_series(series, _before)

                ex = _read_range_one_ex(
                    session, row["concept_id"],
                    row["entity_type"], row["entity_id"],
                    row["min_date"], row["max_date"],
                    revision_reason=REVISION_REASON,
                    # 补救的意义就是"不等 TTL"：这些行是新鲜的（300 天没到），
                    # 正常读会直接命中缓存、永不回源——那样跑一万次也补不回修订。
                    force_refetch=True,
                    admit_series=_admit,
                )
                served = ex.get("served_from")
                if served == "fetch":
                    accepted += 1
                    print(f"  [ok] {key} 已重取并留痕")
                elif served == "write_refused":
                    refused += 1
                    print(f"  [refuse] {key}: {ex.get('refusal_reason')}")
                elif served == "cache_degraded":
                    unavailable += 1
                    print(f"  [unavailable] {key}: 全部来源失败 "
                          f"({', '.join(sorted(set(ex.get('sources_failed') or []))[:3])})")
                else:
                    unchanged += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                # 一条配对失败不许毒化整个批次：不 rollback，后续每条都会
                # 报 InFailedSqlTransaction（实测 40/40 全红，只有第一条是真因）。
                try:
                    session.rollback()
                except Exception:  # noqa: BLE001
                    pass
                print(f"  [fail] {key}: {type(exc).__name__}: {exc}")
            if ckpt_path:
                ckpt_path.write_text(json.dumps({"done": sorted(done | {key})}))
            if i % args.batch_size == 0:
                print(f"  进度 {i}/{len(todo)}（留痕 {accepted} / 拒写 {refused} / "
                      f"取不到 {unavailable} / 无变化 {unchanged} / 失败 {failed}），"
                      f"暂停 {args.sleep_s}s")
                time.sleep(args.sleep_s)

        print()
        print(f"完成：留痕 {accepted} 个，拒写 {refused} 个，取不到 {unavailable} 个，"
              f"无变化 {unchanged} 个，失败 {failed} 个（共 {len(todo)} 个待处理）")
        if refused:
            print("拒写＝上游返回的粒度/量级与既有序列不符，写进去会改坏数据；"
                  "这些配对需要人工确认口径，不能靠重抓解决。")
        print("复核：用 change 的 evidence/README.md 里的核对 SQL 比对该源修订行数变化。")
        return 0 if failed == 0 else 1
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
