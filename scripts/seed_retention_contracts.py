#!/usr/bin/env python3
"""把审计器里的 bootstrap 契约矩阵写入 `data_retention_contracts`（默认 dry-run）。

为什么需要这一步
----------------
`scripts/audit_retention_contracts.py` 里的 `DEFAULT_CONTRACTS` 是**常量**：审计能查，
但写入闸门读的是库表，读不到常量。不 seed，闸门开了就是"全部未声明"，
`enforce` 一开全线停摆。所以顺序必须是：迁移建表 → seed → 观察 warn → 再 enforce。

为什么默认 dry-run
------------------
seed 会把"我对每个族的判断"变成生产数据。写之前必须能看清它要写什么。
带 `--apply` 才落库；已存在的 (source, family) 默认**不覆盖**（`--overwrite` 才改），
因为库里的人工修正比我的 bootstrap 更权威。

用法：
    python scripts/seed_retention_contracts.py --db-url "$DSN"            # 只看会写什么
    python scripts/seed_retention_contracts.py --db-url "$DSN" --apply    # 真写
    python scripts/seed_retention_contracts.py --db-url "$DSN" --apply --overwrite

退出码：0 成功 / 1 有冲突或失败 / 2 用法错误
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent

#: 存量 72 个未声明族的逐条判读（2026-11-11 实测名单，全部 ≤0.8MB）。
#:
#: 分类依据仍是那三个问题，不是"看着像"：
#:   * **F** —— 官方统计/宏观/价格指数：需要长历史，且上游会修订历史
#:     → 必须自己留，as-of 才有依据。
#:   * **P** —— 抓取物与参考数据（LEI、临床试验、披露公告、票房等）：
#:     不可假定可重取，一律按"留"处理；把它当 F 会捏造"随时能再取"这个事实。
#:   * **T / W** —— 只对当下有意义或近期窗口足够的快照（akshare/performance、
#:     polygon 的市值与员工数）。
#:
#: **没有匹配的族一律不写**（打印成"需人工"），不拿默认值填——默认值会让
#: "没想过"和"想过"长得一样，这正是本 change 要消灭的那种状态。
BACKFILL: dict[tuple[str, str], str] = {
    # —— 官方统计 / 宏观 / 价格指数 → F
    ("abs-sdmx", "cons"): "F",
    ("agri-casde", "agriculture"): "F",
    ("amis-market", "agri"): "F",
    ("carbon-monitor", "pwr"): "F",
    ("census-eits", "retail"): "F",
    ("cn-report", "agri"): "F",
    ("cncotton", "agriculture"): "F",
    ("ecb-exr", "fx"): "F",
    ("edgar", "financials"): "F",
    ("edgar-emissions", "pwr"): "F",
    ("eastmoney-f10", "financials"): "F",
    ("ember", "pwr"): "F",
    ("entsoe-e", "pwr"): "F",
    ("fert-cn", "agriculture"): "F",
    ("iea-ev", "auto"): "F",
    ("imf-pcps", "price"): "F",
    ("moa-fishery", "agriculture"): "F",
    ("moa-hog-price", "agri"): "F",
    ("moa-hog-price", "agriculture"): "F",
    ("mohurd-yearbook", "rural"): "F",
    ("mohurd-yearbook", "urban"): "F",
    ("nbs-stats", "agriculture"): "F",
    ("nbs-stats", "retail"): "F",
    ("oecd-cli", "macro"): "F",
    ("pboc-monetary", "macro"): "F",
    ("portwatch-imf", "port"): "F",
    ("tpex-monthly-revenue", "industry"): "F",
    ("twse-monthly-revenue", "industry"): "F",
    ("un-comtrade", "trade"): "F",
    ("usda-quickstats", "agri"): "F",
    ("wbgapi", "NY"): "F",
    ("wbgapi", "SP"): "F",
    ("wbgapi", "gdp"): "F",
    ("wbgapi", "inflation"): "F",
    ("wbgapi", "population"): "F",
    ("wbgapi", "unemployment"): "F",
    ("wto-eping", "policy"): "F",
    ("aiph-stats", "area"): "F",
    # akshare 内的宏观月度序列 → F（不是快照：它们本身就是历史序列）
    ("akshare", "CPI_MOM"): "F", ("akshare", "CPI_YOY"): "F",
    ("akshare", "LPR_1Y"): "F", ("akshare", "LPR_5Y"): "F",
    ("akshare", "M0_AMOUNT"): "F", ("akshare", "M0_YOY"): "F",
    ("akshare", "M1_AMOUNT"): "F", ("akshare", "M1_YOY"): "F",
    ("akshare", "M2_AMOUNT"): "F", ("akshare", "M2_YOY"): "F",
    ("akshare", "PMI_MFG"): "F", ("akshare", "PPI_YOY"): "F",
    # —— 抓取物 / 参考数据 → P（不可假定可重取）
    ("afdc", "energy"): "P",
    ("boxofficemojo", "film"): "P",
    ("chinawuliu-logistics", "expr"): "P",
    ("clinicaltrials", "pharm"): "P",
    ("cninfo-operating", "hotel"): "P",
    ("gleif-lei", "entities"): "P",
    ("hkex-disclosure", "hotel"): "P",
    ("jp-foodservice-trend", "restaurant"): "P",
    ("mct-tourism-demand", "hotel"): "P",
    ("mem-disaster", "disaster"): "P",
    ("nifdc-batchcert", "pharm"): "P",
    ("openalex-works", "research"): "P",
    ("openfda", "pharm"): "P",
    ("restaurant-rpi", "rpi"): "P",
    ("sec-edgar-8k-operating", "hotel"): "P",
    ("socrata-chicago", "transit"): "P",
    ("socrata-ny", "price"): "P",
    ("spb-express", "expr"): "P",
    ("zgdypf-boxoffice", "film"): "P",
    # —— 快照 / 近期窗口 → T、W
    ("akshare", "performance"): "T",
    ("polygon", "employee_count"): "W",   # 窗口 730 天见下
    ("polygon", "market_capitalization"): "W",
}


def _load_audit_module():
    """复用审计器的契约矩阵，避免两处各写一份（不一致会造出自相矛盾的结论）。"""
    spec = importlib.util.spec_from_file_location(
        "audit_retention_contracts", _HERE / "audit_retention_contracts.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["audit_retention_contracts"] = mod   # dataclass 解析注解要用
    spec.loader.exec_module(mod)
    return mod


def _args(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db-url", default=os.environ.get("FD_OPEN_DATA_MCP_DATABASE_URL"))
    p.add_argument("--apply", action="store_true", help="真的写库（默认只打印）")
    p.add_argument("--overwrite", action="store_true",
                   help="覆盖已存在的 (source, family)。默认保留库里的现有值")
    p.add_argument("--backfill-unclassified", action="store_true",
                   help="把 BACKFILL 里逐条判读过的存量族补上契约；未匹配的族不写，列成需人工")
    return p.parse_args(argv)


def _backfill_rows(conn, mod) -> tuple[list, list]:
    """找出库里"有观测行但无契约"的 (源, 族)，用 BACKFILL 判读。

    返回 (可写列表, 需人工列表)。**未匹配的一律进需人工，不拿默认值填**。
    """
    from sqlalchemy import text

    have = {(r._mapping["source"], r._mapping["family"])
            for r in conn.execute(text(
                "SELECT source, family FROM data_retention_contracts"))}

    def covered(src: str, fam: str) -> bool:
        """与 resolve_contract 同口径：精确 → 该源 `*` → 全源 `*` 都算已覆盖。

        少这一层会造出假的"需人工"清单：worldbank / datacommons 有 `*` 契约，
        它们的每个族其实都已声明，只是没有逐族行。
        """
        return (src, fam) in have or (src, "*") in have or ("*", "*") in have

    seen = [r._mapping for r in conn.execute(text("""
        SELECT o.source_used AS src, split_part(co.code, '.', 1) AS fam, count(*) AS n
          FROM semantic_observations o JOIN concepts co ON co.id = o.concept_id
         GROUP BY 1, 2
    """))]

    ready, manual = [], []
    for m in seen:
        key = (m["src"], m["fam"])
        if m["src"] is None or covered(*key):
            continue
        cls = BACKFILL.get(key)
        if cls is None:
            manual.append((m["src"], m["fam"], m["n"]))
        else:
            ready.append((m["src"], m["fam"], cls, None, None, None, 64))
    return ready, manual


def main(argv=None) -> int:
    a = _args(argv)
    if not a.db_url:
        print("缺少 --db-url（或 FD_OPEN_DATA_MCP_DATABASE_URL）", file=sys.stderr)
        return 2

    mod = _load_audit_module()
    rows = [(src, fam, cls, win, refetch, mutable, budget)
            for (src, fam, cls, win, refetch, mutable, budget) in mod.DEFAULT_CONTRACTS]

    from sqlalchemy import create_engine, text

    eng = create_engine(a.db_url)
    try:
        with eng.begin() as c:
            if a.backfill_unclassified:
                rows, manual = _backfill_rows(c, mod)
                print(f"存量未声明族中，已判读 {len(rows)} 个可补；"
                      f"{len(manual)} 个无规则、留人工（不猜）")
                print()
                print(f"{'源':<24}{'族':<18}{'类':>3}{'预算MB':>8}")
                print("-" * 56)
                for src, fam, cls, _win, _rf, _mut, budget in rows:
                    print(f"{src:<24}{fam:<18}{cls:>3}{budget:>8}")
                if manual:
                    print()
                    print("需人工判读（本次不写）：")
                    for src, fam, n in manual[:10]:
                        print(f"  {src}/{fam}  rows={n}")
                    if len(manual) > 10:
                        print(f"  …共 {len(manual)} 个")
                if not a.apply:
                    print()
                    print("（dry-run：以上只是计划，未写库。加 --apply 执行。）")
                    return 0
                for src, fam, cls, _win, _rf, _mut, budget in rows:
                    c.execute(text("""
                        INSERT INTO data_retention_contracts
                          (source, family, retention_class, volume_budget_mb, note)
                        VALUES (:s, :f, :cls, :budget, :note)
                        ON CONFLICT (source, family) DO NOTHING
                    """), {"s": src, "f": fam, "cls": cls, "budget": budget,
                           "note": "backfill 2026-11-11（逐条判读，未匹配者留人工）"})
                total = c.execute(text(
                    "SELECT count(*) FROM data_retention_contracts")).scalar()
                print()
                print(f"已补写。表内现有 {total} 条契约。")
                return 0

            existing = {
                (r._mapping["source"], r._mapping["family"])
                for r in c.execute(text(
                    "SELECT source, family FROM data_retention_contracts"))
            }
            to_insert = [r for r in rows if a.overwrite or (r[0], r[1]) not in existing]
            print(f"bootstrap 契约 {len(rows)} 条；库里已有 {len(existing)} 条；"
                  f"本次{'覆盖写入' if a.overwrite else '新增'} {len(to_insert)} 条")
            print()
            print(f"{'源':<20}{'族':<18}{'类':>3}{'窗口':>7}{'可重取':>7}{'预算MB':>8}")
            print("-" * 66)
            for src, fam, cls, win, refetch, mutable, budget in to_insert:
                print(f"{src:<20}{fam:<18}{cls:>3}{win or '-':>7}"
                      f"{'是' if refetch else '否':>7}{budget:>8}")

            if not a.apply:
                print()
                print("（dry-run：以上只是计划，未写库。加 --apply 执行。）")
                return 0

            for src, fam, cls, win, refetch, mutable, budget in to_insert:
                if a.overwrite:
                    c.execute(text("""
                        INSERT INTO data_retention_contracts
                          (source, family, retention_class, retention_window_days,
                           re_fetchable, upstream_history_mutable, volume_budget_mb, note)
                        VALUES (:s, :f, :cls, :win, :rf, :mut, :budget, :note)
                        ON CONFLICT (source, family) DO UPDATE SET
                          retention_class = EXCLUDED.retention_class,
                          retention_window_days = EXCLUDED.retention_window_days,
                          re_fetchable = EXCLUDED.re_fetchable,
                          upstream_history_mutable = EXCLUDED.upstream_history_mutable,
                          volume_budget_mb = EXCLUDED.volume_budget_mb,
                          updated_at = now()
                    """), {"s": src, "f": fam, "cls": cls, "win": win, "rf": refetch,
                           "mut": mutable, "budget": budget,
                           "note": "seed from audit bootstrap matrix"})
                elif (src, fam) not in existing:
                    c.execute(text("""
                        INSERT INTO data_retention_contracts
                          (source, family, retention_class, retention_window_days,
                           re_fetchable, upstream_history_mutable, volume_budget_mb, note)
                        VALUES (:s, :f, :cls, :win, :rf, :mut, :budget, :note)
                    """), {"s": src, "f": fam, "cls": cls, "win": win, "rf": refetch,
                           "mut": mutable, "budget": budget,
                           "note": "seed from audit bootstrap matrix"})

            total = c.execute(text(
                "SELECT count(*) FROM data_retention_contracts")).scalar()
            print()
            print(f"已写入。表内现有 {total} 条契约。")
    except Exception as exc:  # noqa: BLE001
        print(f"失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        eng.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
