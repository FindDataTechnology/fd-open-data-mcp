#!/usr/bin/env python3
"""留存契约审计：把「什么该留多久」变成可复跑的检查（**只读，不改数据**）。

对应 `establish-data-retention-principle` 的 R1（每源须有契约）、R7（体量可观测）
以及最关键的**防复发判据**：被定为 T 类（只留当期）的数据族，若仍在逐日积累历史行，
就是"契约写了但没人执行"，必须当场报警——否则清完几个月又长回来。

判据（三个正交问题，不看"重不重要"）：
    Q1 上游会改历史吗 → 会改就必须自己留，as-of 才有依据
    Q2 分析需要多深历史 → 要长历史 = F / 只要当期 = T
    Q3 再取一次多贵 → 一次 HTTP 可透传 / 需重建要存 / 不可重建永久存

四类处置：P 永久、F 全历史、W 滚动窗口、T 只留当期。

用法：
    python scripts/audit_retention_contracts.py --db-url "$DSN"
    python scripts/audit_retention_contracts.py --db-url "$DSN" --json

退出码：0 无违规 / 1 有容量违规（要立刻处理）/ 3 仅契约待补 / 2 未知（读不到库或解析失败
—— **未知不等于通过**）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass

#: 平均行字节（实测 355 B/行，含索引）。仅用于把行数折算成体量口径。
AVG_ROW_BYTES = 355

#: 契约默认矩阵：(source, family 前缀) → 类别 / 窗口天数 / 可重取 / 上游会改历史 / 预算MB
#:
#: 这些值来自 design.md 的决策矩阵与生产实测（2026-11-11）。落在数据库之前先放在这里，
#: 是为了让审计**现在**就能跑；契约表落库后应改为读表，此处只作 bootstrap 默认。
P, F, W, T = "P", "F", "W", "T"
DEFAULT_CONTRACTS: tuple[tuple[str, str, str, int | None, bool, bool, int], ...] = (
    # 永久：不可重建 / 我们的产物
    ("*", "crawl", P, None, False, True, 512),
    # financials 全历史（退市公司历史要去幸存者偏差）
    ("akshare", "financials", F, None, True, True, 3072),
    ("cnreport", "financials", F, None, True, True, 64),
    ("cn-report", "financials", F, None, True, True, 64),
    ("gta-panel", "financials", F, None, True, True, 64),
    ("sec-edgar-facts", "financials", F, None, True, True, 64),
    # 宏观/官方统计：要长历史 + 上游会修订
    ("datacommons", "*", F, None, True, True, 128),
    ("worldbank", "*", F, None, True, True, 64),
    ("fao-food-price", "*", F, None, True, True, 64),
    ("fred-cpi", "*", F, None, True, True, 64),
    ("dbnomics-api", "*", F, None, True, True, 64),
    ("eurostat-api", "*", F, None, True, True, 64),
    ("eia-v2", "*", F, None, True, True, 64),
    ("dce", "*", F, None, True, True, 64),
    ("ico-coffee-price", "*", F, None, True, True, 64),
    ("dounan-flower-price", "*", F, None, False, True, 64),
    ("mct-star-hotel", "*", F, None, False, True, 64),
    # 滚动窗口
    ("akshare", "aum", W, 1826, True, True, 512),
    ("polygon", "price", W, 730, True, True, 128),
    ("polygon", "trading_volume", W, 730, True, True, 128),
    ("akshare", "price", W, 730, True, True, 128),
    # T：只留当期（快照型派生指标）——防复发判据主要盯这一组
    ("akshare", "tenure_days", T, 30, True, False, 64),
    ("akshare", "best_return", T, 30, True, False, 64),
    ("akshare", "aum_total", T, 30, True, False, 64),
    ("akshare", "funds_count", T, 30, True, False, 64),
    ("akshare", "return", T, 30, True, False, 64),
    ("akshare", "rating", T, 30, True, False, 64),
    ("akshare", "yield", T, 30, True, False, 64),
    ("akshare", "nav", T, 30, True, False, 64),
)


@dataclass(frozen=True)
class Contract:
    source: str
    family: str
    cls: str
    window_days: int | None
    re_fetchable: bool
    upstream_history_mutable: bool
    budget_mb: int


def resolve_contract(source: str | None, family: str | None, db_contracts=None) -> Contract | None:
    """按 (source, family) 找契约：**先库表，再 bootstrap 常量**。

    优先级不能反：`data_retention_contracts` 是制度落点（有人可以改它、also 有 note 字段），
    而 `DEFAULT_CONTRACTS` 只是 seed 之前的引导值。若反过来读，DBA 在库里改的契约会被
    代码常量静默覆盖——那是"改了配置没生效"的经典坑。
    """
    s = source or "(null)"
    f = family or "(null)"
    for cand in ((s, f), (s, "*"), ("*", "*")):
        if db_contracts:
            for key, c in db_contracts.items():
                if key == cand:
                    return c
        for src, fam, cls, win, refetch, mut, budget in DEFAULT_CONTRACTS:
            if (src, fam) == cand:
                return Contract(s, f, cls, win, refetch, mut, budget)
    return None


def load_db_contracts(conn) -> dict:
    """从 `data_retention_contracts` 读契约；表不存在时返回空（迁移未跑也要能审）。"""
    from sqlalchemy import text

    try:
        rows = conn.execute(text("""
            SELECT source, family, retention_class, retention_window_days,
                   re_fetchable, upstream_history_mutable, volume_budget_mb
              FROM data_retention_contracts
        """)).all()
    except Exception:  # noqa: BLE001 — 表还没建：退到 bootstrap 常量
        return {}
    out = {}
    for r in rows:
        m = r._mapping
        if not m["retention_class"]:
            continue   # class 为空的契约等于未声明，不参与解析
        out[(m["source"], m["family"])] = Contract(
            m["source"], m["family"], m["retention_class"],
            m["retention_window_days"], m["re_fetchable"],
            m["upstream_history_mutable"], m["volume_budget_mb"] or 0,
        )
    return out


def classify_violations(contract: Contract | None, *, rows: int, over_window: int,
                        rows_per_entity_recent: float, family: str) -> list[str]:
    """纯函数：给一个数据族的读数，返回违规原因列表（空 = 合规）。

    这是整套原则里最要紧的一段：**它能抓住"契约写了但没人执行"**。
    T 类声明只留当期，若仍在逐日堆积（近期每实体多行），就是违规——否则清完又长回来。
    """
    out: list[str] = []
    if contract is None:
        return ["no_contract：该 (源, 族) 没有留存契约，按原则不得继续写入"]
    if rows > 0 and contract.budget_mb and rows * AVG_ROW_BYTES > contract.budget_mb * 1024 * 1024:
        out.append(f"over_budget：约 {rows * AVG_ROW_BYTES // (1024 * 1024)}MB "
                   f"超过预算 {contract.budget_mb}MB")
    if contract.cls == T and rows_per_entity_recent > 1.5:
        out.append(f"T_class_accruing_history：T 类声明只留当期，但近 7 天每实体平均 "
                   f"{rows_per_entity_recent:.1f} 行——仍在逐日堆积历史")
    if contract.cls in (W, T) and over_window > 0:
        out.append(f"over_window：{over_window} 行超出 {contract.window_days} 天窗口，应淘汰或给出豁免")
    if contract.cls == P and contract.window_days is not None:
        # 矛盾在**契约本身**：P 是不可回收，声明窗口即自相矛盾。
        # 不能拿 over_window 判——P 类有五年以上的老行恰恰是对的。2026-11-11 实测：
        # 旧判据把 7 个 P 族（gleif-lei、openalex-works、openfda…）全误报成"自相矛盾"。
        out.append("P_class_has_window：P 类不可回收，不应声明窗口字段")
    return out


def severity_of(violations: list[str]) -> str:
    """把违规分成两档，因为它们要的是不同的处置，混在一起会让信号失效。

    * ``capacity``     —— 真的在吃盘/违约：T 类仍在堆积、超预算、超窗口。
                          这类必须立刻处理，CI 应当因此红灯。
    * ``contract_gap`` —— 只是"契约还没声明"（``no_contract``）。它违反 R1，但历史源
                          本来就早于契约制度，是待补清单，不是容量事故。
    * ``ok``           —— 无违规。
    """
    if not violations:
        return "ok"
    if any(not v.startswith("no_contract") for v in violations):
        return "capacity"
    return "contract_gap"


def _args(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db-url", default=os.environ.get("FD_OPEN_DATA_MCP_DATABASE_URL"))
    p.add_argument("--json", action="store_true", help="输出 JSON")
    return p.parse_args(argv)


def main(argv=None) -> int:
    a = _args(argv)
    if not a.db_url:
        print("缺少 --db-url（或 FD_OPEN_DATA_MCP_DATABASE_URL）", file=sys.stderr)
        return 2
    try:
        from sqlalchemy import create_engine, text
    except Exception as exc:  # noqa: BLE001
        print(f"UNKNOWN: 无法导入 sqlalchemy：{exc}", file=sys.stderr)
        return 2

    eng = create_engine(a.db_url)
    try:
        with eng.connect() as c:
            db_contracts = load_db_contracts(c)
            rows = c.execute(text("""
                SELECT o.source_used AS src,
                       split_part(co.code, '.', 1) AS fam,
                       count(*) AS n,
                       count(DISTINCT (o.entity_type, o.entity_id)) AS entities,
                       min(o.date) AS d0, max(o.date) AS d1
                  FROM semantic_observations o
                  JOIN concepts co ON co.id = o.concept_id
                 GROUP BY 1, 2
                 ORDER BY n DESC
            """)).all()
            recent = c.execute(text("""
                SELECT o.source_used AS src,
                       split_part(co.code, '.', 1) AS fam,
                       count(*)::float / GREATEST(count(DISTINCT (o.entity_type, o.entity_id)), 1) AS per_entity
                  FROM semantic_observations o
                  JOIN concepts co ON co.id = o.concept_id
                 WHERE o.fetched_at > now() - interval '7 days'
                 GROUP BY 1, 2
            """)).all()
            thresholds = c.execute(text("""
                SELECT o.source_used AS src,
                       split_part(co.code, '.', 1) AS fam,
                       count(*) FILTER (WHERE o.date < to_char(now() - interval '1826 days', 'YYYY-MM-DD')) AS over_5y
                  FROM semantic_observations o
                  JOIN concepts co ON co.id = o.concept_id
                 GROUP BY 1, 2
            """)).all()
    except Exception as exc:  # noqa: BLE001
        print(f"UNKNOWN: 查询失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    finally:
        eng.dispose()

    per_entity = {(r._mapping["src"], r._mapping["fam"]): r._mapping["per_entity"] for r in recent}
    over5 = {(r._mapping["src"], r._mapping["fam"]): r._mapping["over_5y"] for r in thresholds}

    report, capacity, gaps = [], 0, 0
    for r in rows:
        m = r._mapping
        contract = resolve_contract(m["src"], m["fam"], db_contracts)
        pe = per_entity.get((m["src"], m["fam"]), 0.0)
        ov = over5.get((m["src"], m["fam"]), 0) or 0
        v = classify_violations(contract, rows=m["n"], over_window=ov,
                               rows_per_entity_recent=pe, family=m["fam"])
        sev = severity_of(v)
        if sev == "capacity":
            capacity += 1
        elif sev == "contract_gap":
            gaps += 1
        report.append({
            "source": m["src"], "family": m["fam"], "rows": m["n"],
            "mb": round(m["n"] * AVG_ROW_BYTES / 1024 / 1024, 1),
            "entities": m["entities"], "span": f"{m['d0']}..{m['d1']}",
            "class": contract.cls if contract else None,
            "window_days": contract.window_days if contract else None,
            "over_5y_rows": ov, "rows_per_entity_7d": round(pe, 2),
            "severity": sev, "violations": v,
        })

    if a.json:
        print(json.dumps({"entries": report, "capacity_count": capacity,
                          "contract_gap_count": gaps}, ensure_ascii=False, indent=2))
    else:
        print("== 容量违规（要立刻处理，CI 应红灯）==")
        print(f"{'源':<18}{'族':<16}{'行数':>10}{'MB':>7}{'类':>3}{'近7天/实体':>11}  原因")
        print("-" * 90)
        shown = 0
        for e in report:
            if e["severity"] != "capacity":
                continue
            shown += 1
            print(f"{e['source']:<18}{e['family']:<16}{e['rows']:>10,}{e['mb']:>7}"
                  f"{e['class'] or '?':>3}{e['rows_per_entity_7d']:>11.2f}  "
                  f"{'; '.join(e['violations'])[:52]}")
        if not shown:
            print("  （无）")
        print()
        print("== 契约待补（违反 R1，但属存量待补，不是容量事故）==")
        gap_rows = [e for e in report if e["severity"] == "contract_gap"]
        gap_rows.sort(key=lambda e: -e["rows"])
        for e in gap_rows[:8]:
            print(f"{e['source']:<18}{e['family']:<16}{e['rows']:>10,}{e['mb']:>7}MB")
        if len(gap_rows) > 8:
            print(f"  …共 {len(gap_rows)} 个，合计 "
                  f"{round(sum(e['mb'] for e in gap_rows), 1)}MB（最大一个 "
                  f"{gap_rows[0]['mb']}MB）")
        print()
        print(f"共 {len(report)} 个 (源, 族)：容量违规 {capacity} 个，契约待补 {gaps} 个")

    if capacity:
        return 1
    return 3 if gaps else 0


if __name__ == "__main__":
    raise SystemExit(main())
