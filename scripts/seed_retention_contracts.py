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
    return p.parse_args(argv)


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
