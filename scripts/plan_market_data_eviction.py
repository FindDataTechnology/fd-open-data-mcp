#!/usr/bin/env python3
"""盘点「可重取数据」占了多少，供存储策略决策（**只读，绝不删除**）。

背景
----
盘的容量被"随时能从第三方再取"的数据占着，而它同时还要付备份/DR 成本——这是净损失。
用户判断（2026-11-11）：行情这类容易获取的数据，没必要自己长期存。

实测（100.64.0.3 / fd_open_data，2026-11-11）：

    db_size 4,632 MB，semantic_observations 3,379 MB
    akshare / quarter  7,304,318 行（73.2%）
    akshare / day      2,265,543 行（22.7%）
    akshare / year       236,980 行（ 2.4%）
    → akshare 一个源就占 98.4% 的行

为什么这个脚本只报告、不删除
----------------------------
删除已定型观测与本项目 R1（定型后不可变 + 修订留痕 + as-of 可回放）是**规格级**冲突：
一旦把某段行情删掉，那段 as-of 回放就只能返回 `vintage_unavailable`/`absent`，而
`absent` 与"当时本来就没有"长得一模一样。所以"哪些源、哪段历史可以不再保留"必须
由 change 明确声明保留边界，不能由这个脚本顺手决定。

本脚本因此只做三件事：
  1. 按 (source, granularity) 给出**行数与估算字节**；
  2. 给出每个候选范围的**概念/实体数与日期跨度**（判断"删掉会不会毁掉某条序列"）；
  3. 打印它**本会执行**的 SQL（`--show-sql`），让删除动作可以被独立审阅。

用法：
    python scripts/plan_market_data_eviction.py --db-url "$DSN"
    python scripts/plan_market_data_eviction.py --db-url "$DSN" --source akshare --granularity quarter
    python scripts/plan_market_data_eviction.py --db-url "$DSN" --show-sql
"""

from __future__ import annotations

import argparse
import os
import sys

#: 默认可重取来源——这些是"第三方随时能再取"的行情/财报口径。
REFETCHABLE_SOURCES = ("akshare", "polygon", "cnreport", "cn-report", "dce")

#: 常见粒度。
GRANULARITIES = ("quarter", "day", "month", "year")


def _args(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db-url", default=os.environ.get("FD_OPEN_DATA_MCP_DATABASE_URL"))
    p.add_argument("--source", action="append",
                   help="只看这些来源（可重复）。默认列出全部")
    p.add_argument("--granularity", action="append",
                   help="只看这些粒度（可重复）。默认列出全部")
    p.add_argument("--older-than-days", type=int, default=0,
                   help="只看观测期在 N 天之前的行（0 = 不限）")
    p.add_argument("--show-sql", action="store_true",
                   help="打印本会执行的 DELETE 语句（不执行）")
    return p.parse_args(argv)


def human(n: float) -> str:
    """把字节数说成人话。GB/TB 保留一位小数——这是给容量决策看的，
    整数量级会把 4.52GB 报成 "5GB"，凭空多出半个 G。"""
    for unit in ("B", "KB", "MB"):
        if abs(n) < 1024:
            return f"{n:.0f}{unit}"
        n /= 1024
    return f"{n:.1f}GB" if abs(n) < 1024 else f"{n / 1024:.1f}TB"


def main(argv=None) -> int:
    a = _args(argv)
    if not a.db_url:
        print("缺少 --db-url（或 FD_OPEN_DATA_MCP_DATABASE_URL）", file=sys.stderr)
        return 2

    # 用裸 engine，不走 Database：那是写入面的入口（会触发 schema gate），
    # 这里只需要读。
    from sqlalchemy import create_engine, text

    eng = create_engine(a.db_url)
    where, params = [], {}
    if a.source:
        where.append("source_used = ANY(:srcs)")
        params["srcs"] = list(a.source)
    if a.granularity:
        where.append("granularity = ANY(:grans)")
        params["grans"] = list(a.granularity)
    if a.older_than_days:
        where.append("fetched_at < now() - (:days || ' days')::interval")
        params["days"] = str(a.older_than_days)
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    with eng.connect() as c:
        db_size = c.execute(text("select pg_database_size(current_database())")).scalar()
        tbl = c.execute(text(
            "select pg_total_relation_size('semantic_observations')")).scalar()
        total_rows = c.execute(text("select count(*) from semantic_observations")).scalar()
        if not total_rows:
            print("semantic_observations 为空，无事可做")
            return 0
        avg_row = tbl / total_rows

        rows = c.execute(text(f"""
            select coalesce(source_used,'(null)') as src,
                   coalesce(granularity,'(null)') as gran,
                   count(*) as n
              from semantic_observations{clause}
             group by 1,2 order by n desc
        """), params).all()

        print(f"库总量 {human(db_size)}，semantic_observations {human(tbl)}"
              f"（{total_rows:,} 行，均 {avg_row:.0f} B/行）")
        print()
        print(f"{'来源':<14}{'粒度':<10}{'行数':>12}{'占全表':>9}{'估算字节':>12}")
        print("-" * 58)
        sub_rows = 0
        for r in rows:
            m = r._mapping
            sub_rows += m["n"]
            print(f"{m['src']:<14}{m['gran']:<10}{m['n']:>12,}"
                  f"{100.0 * m['n'] / total_rows:>8.1f}%{human(m['n'] * avg_row):>12}")
        print("-" * 58)
        print(f"{'合计':<24}{sub_rows:>12,}{100.0 * sub_rows / total_rows:>8.1f}%"
              f"{human(sub_rows * avg_row):>12}")

        # 每个 (source, granularity) 的序列广度：删之前要知道会动到多少条序列
        print()
        print("候选范围的序列广度（概念数 / 实体数 / 日期跨度 / 已有修订数）：")
        for r in rows[:12]:
            m = r._mapping
            sub = []
            sp = dict(params)
            if m["src"] == "(null)":
                sub.append("source_used IS NULL")
            else:
                sub.append("source_used = :s")
                sp["s"] = m["src"]
            if m["gran"] == "(null)":
                sub.append("granularity IS NULL")
            else:
                sub.append("granularity = :g")
                sp["g"] = m["gran"]
            w = " AND ".join(sub)
            dev = c.execute(text(f"""
                select count(distinct concept_id) as c,
                       count(distinct (entity_type, entity_id)) as e,
                       min(date) as d0, max(date) as d1
                  from semantic_observations where {w}
            """), sp).one()._mapping
            revs = c.execute(text(f"""
                select count(*) from observation_revisions r
                 where exists (select 1 from semantic_observations o
                                where o.concept_id = r.concept_id
                                  and o.entity_type = r.entity_type
                                  and o.entity_id = r.entity_id and {w.replace('source_used','o.source_used').replace('granularity','o.granularity')})
            """), sp).scalar()
            print(f"  {m['src']}/{m['gran']:<8} 概念 {dev['c']:>5}  实体 {dev['e']:>6}  "
                  f"{dev['d0']}..{dev['d1']}  修订 {revs}")

        if a.show_sql:
            print()
            print("-- 本脚本**不会**执行下面这条；它只是让删除动作可被独立审阅。")
            print("-- 注意：删掉已定型行会让该段 as-of 回放变成 vintage_unavailable/absent，")
            print("-- 这在 R1 语境下是规格级决定，必须先由 change 声明保留边界。")
            print("DELETE FROM semantic_observations" + (clause or " /* 全表，危险 */") + ";")
            if params:
                print(f"-- 绑定参数：{params}")

        _contract_report(c, avg_row, only_source=a.source)

    eng.dispose()
    return 0


def _contract_report(conn, avg_row: float, only_source=None) -> None:
    """按留存契约报账：每个 (源, 族) 的行数 / 字节 / 广度 / 超窗 / 预算占用。

    与前半段的 (source, granularity) 视角互补：粒度回答"多大"，契约回答"该不该留"。
    只看行数会得出"akshare 占 98%"这种结论，但它没说清其中哪些是**本来就可以不存**的。

    只读。不打印任何删除语句——淘汰一律走 `evict_retention_window.py`（先搬后删）。
    """
    from sqlalchemy import text

    sql = """
        SELECT o.source_used AS src, split_part(c.code, '.', 1) AS fam,
               count(*) AS n,
               count(DISTINCT o.concept_id) AS concepts,
               count(DISTINCT (o.entity_type, o.entity_id)) AS entities,
               min(o.date) AS d0, max(o.date) AS d1
          FROM semantic_observations o JOIN concepts c ON c.id = o.concept_id
         GROUP BY 1, 2
    """
    params = {}
    if only_source:
        sql = sql.replace("GROUP BY 1, 2",
                          "WHERE o.source_used = ANY(:srcs) GROUP BY 1, 2")
        params["srcs"] = list(only_source)
    rows = conn.execute(text(sql), params).all()

    contracts = {}
    for r in conn.execute(text("""
        SELECT source, family, retention_class, retention_window_days,
               re_fetchable, volume_budget_mb
          FROM data_retention_contracts
    """)):
        contracts[(r.source, r.family)] = r

    print()
    print("== 按留存契约报账（只读；『可释放』= 超出契约窗口的行）==")
    print(f"{'源/族':<34}{'类':<3}{'窗口':>7}{'行数':>11}{'MB':>8}"
          f"{'概念':>7}{'实体':>8}{'可释放':>10}{'占本族':>8}{'预算占用':>10}")
    print("-" * 108)

    total_rows = total_free = 0
    free_mb = 0.0
    over_budget = []
    unclassified = []
    for m in rows:
        ct = contracts.get((m.src, m.fam)) or contracts.get((m.src, "*"))
        if ct is None:
            unclassified.append((m.src, m.fam, m.n))
            continue
        free = 0
        if ct.retention_class == "T":
            # T：每 (概念, 实体) 只该留一条，其余都是可释放的
            dups = conn.execute(text("""
                SELECT coalesce(sum(n - 1), 0) FROM (
                  SELECT count(*) AS n FROM semantic_observations o
                    JOIN concepts c ON c.id = o.concept_id
                   WHERE o.source_used = :src AND split_part(c.code, '.', 1) = :fam
                   GROUP BY o.concept_id, o.entity_type, o.entity_id
                ) t
            """), {"src": m.src, "fam": m.fam}).scalar() or 0
            free = int(dups)
        elif ct.retention_class == "W" and ct.retention_window_days:
            free = conn.execute(text("""
                SELECT count(*) FROM semantic_observations o
                  JOIN concepts c ON c.id = o.concept_id
                 WHERE o.source_used = :src AND split_part(c.code, '.', 1) = :fam
                   AND o.date < to_char(now() - (:d || ' days')::interval, 'YYYY-MM-DD')
            """), {"src": m.src, "fam": m.fam,
                   "d": str(ct.retention_window_days)}).scalar() or 0
            free = int(free)

        mb = m.n * avg_row / 1024 / 1024
        free_mb += free * avg_row / 1024 / 1024
        total_rows += m.n
        total_free += free
        budget = ct.volume_budget_mb or 0
        occ = (mb / budget * 100) if budget else 0.0
        if budget and mb > budget:
            over_budget.append((m.src, m.fam, mb, budget))
        print(f"{m.src + '/' + m.fam:<34}{ct.retention_class:<3}"
              f"{(str(ct.retention_window_days) + 'd' if ct.retention_window_days else '—'):>7}"
              f"{m.n:>11,}{mb:>8.1f}{m.concepts:>7}{m.entities:>8}"
              f"{free:>10,}{(100.0 * free / m.n if m.n else 0):>7.1f}%"
              f"{(f'{occ:.0f}% / {budget}MB' if budget else '—'):>10}")

    print("-" * 108)
    print(f"{'合计':<44}{total_rows:>11,}{'':>8}{'':>7}{'':>8}{total_free:>10,}"
          f"  ≈ 可释放 {free_mb:.1f}MB")

    if over_budget:
        print()
        print("== 超预算（与磁盘余量无关；盘再空也算违规）==")
        for src, fam, mb, budget in sorted(over_budget, key=lambda x: -x[2]):
            print(f"  {src}/{fam}: {mb:.1f}MB > 预算 {budget}MB")

    if unclassified:
        print()
        print(f"== 无契约的族 {len(unclassified)} 个（"
              f"{sum(n for *_, n in unclassified):,} 行）——先补契约，再谈取舍 ==")
        for src, fam, n in sorted(unclassified, key=lambda x: -x[2])[:10]:
            print(f"  {src}/{fam}: {n:,} 行")


if __name__ == "__main__":
    raise SystemExit(main())
