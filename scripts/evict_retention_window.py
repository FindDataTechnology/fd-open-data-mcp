#!/usr/bin/env python3
"""按留存契约做窗口淘汰：把该走的行搬进 quarantine（**默认 dry-run，不删**）。

它回答一个问题：**契约说了留多久，那超出部分现在具体是哪几行、多少字节？**

设计取舍（都是前几轮踩出来的）
------------------------------
* **先搬后删，不直接 DELETE**：目标表是 `semantic_observations_quarantine`（已存在，
  含 351,750 行）。搬过去是**可逆**的——回滚只需反向搬回，所以这一步不需要"删了就没了"
  的心理负担。
* **沿用原 id**：实测两表 id 无交集（`overlap=0`），所以隔离行保留原 id，回滚能精确到行。
  写入用 `ON CONFLICT (id) DO NOTHING`，冲突则不搬、**也不删**（宁可留着，不可丢）。
* **只动 T 与 W**：P（不可回收）与 F（全历史）一行不碰；没有契约的族也不碰——
  "不知道"不等于"可以删"。
* **T 类只留最新一点**：`tenure_days` 这类是派生快照，同一实体只保留 `date` 最大的那条；
  变化痕迹本来就在 `observation_revisions` 里，不靠这些重复行保存。
* **W 类按窗口切**：`date < 今天 − retention_window_days`。

用法：
    python scripts/evict_retention_window.py --db-url "$DSN"                  # 只看计划
    python scripts/evict_retention_window.py --db-url "$DSN" --family akshare/tenure_days
    python scripts/evict_retention_window.py --db-url "$DSN" --apply --batch-size 5000

退出码：0 无待淘汰 / 1 有待淘汰（dry-run 或已执行）/ 2 未知或失败
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent
AVG_ROW_BYTES = 355   # 与审计器同一口径（实测均 355 B/行）

_LOAD_COLS = ("id, concept_id, entity_type, entity_id, date, granularity, "
              "value, unit, source_used, fetched_at, obs_month")


def _load_audit():
    spec = importlib.util.spec_from_file_location(
        "audit_retention_contracts", _HERE / "audit_retention_contracts.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["audit_retention_contracts"] = mod
    spec.loader.exec_module(mod)
    return mod


def _args(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db-url", default=os.environ.get("FD_OPEN_DATA_MCP_DATABASE_URL"))
    p.add_argument("--family", action="append",
                   help="只处理这些 源/族（如 akshare/tenure_days，可重复）")
    p.add_argument("--batch-size", type=int, default=5000)
    p.add_argument("--apply", action="store_true", help="真的搬（默认只统计）")
    p.add_argument("--limit-rows", type=int, default=0,
                   help="最多搬多少行（0=不限），用于小范围试跑")
    return p.parse_args(argv)


def _victims_sql(cls: str) -> str:
    """返回挑出待淘汰行的 SQL（占位 :src / :fam / :cutoff 由调用方补）。

    T：同一 (概念, 实体) 只留 date 最大的一条。
    W：date 早于窗口下界的全走。
    """
    if cls == "T":
        return """
            SELECT id FROM (
              SELECT o.id,
                     row_number() OVER (
                       PARTITION BY o.concept_id, o.entity_type, o.entity_id
                       ORDER BY o.date DESC, o.fetched_at DESC
                     ) AS rn
                FROM semantic_observations o
                JOIN concepts c ON c.id = o.concept_id
               WHERE o.source_used = :src
                 AND split_part(c.code, '.', 1) = :fam
            ) t WHERE rn > 1
        """
    if cls == "W":
        return """
            SELECT o.id FROM semantic_observations o
              JOIN concepts c ON c.id = o.concept_id
             WHERE o.source_used = :src
               AND split_part(c.code, '.', 1) = :fam
               AND o.date < :cutoff
        """
    raise ValueError(f"只支持 T/W，收到 {cls}")


def _row_counts(conn) -> dict[str, int]:
    """三张关键表的行数——用于"搬运前后必须对得上账"的不变式。"""
    from sqlalchemy import text

    out = {}
    for name in ("semantic_observations", "semantic_observations_quarantine",
                 "observation_revisions"):
        try:
            out[name] = conn.execute(text(f"SELECT count(*) FROM {name}")).scalar() or 0
        except Exception:  # noqa: BLE001 - 隔离表可能不存在
            out[name] = -1
    return out


def _assert_balanced(before: dict[str, int], after: dict[str, int],
                     moved: int) -> None:
    """4.2 不变式：观测恰减 moved、隔离恰加 moved、修订表一行不动。

    这个工具是唯一会减少观测行数的地方；少一行是丢数据，多一行是漏处理，
    两者都必须当场炸掉而不是打印一句"已搬 N 行"就算完。
    """
    d_obs = before["semantic_observations"] - after["semantic_observations"]
    if d_obs != moved:
        raise RuntimeError(
            f"行数不变式失败：观测减少 {d_obs:,} 但声明搬运 {moved:,} 行"
            f"（差额 {d_obs - moved:+,}）——事务已回滚，请查并发写入")
    if after["semantic_observations_quarantine"] >= 0:
        d_q = (after["semantic_observations_quarantine"]
               - before["semantic_observations_quarantine"])
        if d_q != moved:
            raise RuntimeError(
                f"行数不变式失败：隔离表增加 {d_q:,} 但声明搬运 {moved:,} 行"
                f"（差额 {d_q - moved:+,}）——事务已回滚")
    d_rev = after["observation_revisions"] - before["observation_revisions"]
    if d_rev != 0:
        raise RuntimeError(
            f"行数不变式失败：observation_revisions 变化 {d_rev:+,} 行——"
            "搬运不应产生或删除修订记录，事务已回滚")
    print(f"  行数对账通过：观测 −{moved:,} / 隔离 +{moved:,} / 修订 ±0")


def main(argv=None) -> int:
    a = _args(argv)
    if not a.db_url:
        print("缺少 --db-url（或 FD_OPEN_DATA_MCP_DATABASE_URL）", file=sys.stderr)
        return 2

    audit = _load_audit()
    from sqlalchemy import create_engine, text

    eng = create_engine(a.db_url)
    try:
        with eng.connect() as c:
            contracts = audit.load_db_contracts(c)

        # 找出库里实际存在、且契约为 T 或 W 的 (源, 族)
        with eng.connect() as c:
            present = [r._mapping for r in c.execute(text("""
                SELECT o.source_used AS src, split_part(co.code, '.', 1) AS fam,
                       count(*) AS n
                  FROM semantic_observations o JOIN concepts co ON co.id = o.concept_id
                 GROUP BY 1, 2
            """))]

        targets = []
        for m in present:
            key = (m["src"], m["fam"])
            if a.family and f"{m['src']}/{m['fam']}" not in a.family:
                continue
            ct = audit.resolve_contract(m["src"], m["fam"], contracts)
            if ct is None or ct.cls not in ("T", "W"):
                continue      # P / F / 无契约：一律不碰
            if ct.re_fetchable is False:
                # 4.1 硬闸：明确声明"取不回来"的层，一行都不许动。
                # 契约自相矛盾（类写着 T/W 该删、可重取性写着 false 删了就没了）时，
                # 唯一正确的动作是停下并交给人，不是替人挑一个。
                print(f"  拒绝 {m['src']}/{m['fam']}：契约声明 re_fetchable=false"
                      f"（不可回收层，淘汰不可逆）——策略不得触及，需人工改契约")
                continue
            if ct.re_fetchable is None:
                # 未声明 ≠ 不可回收。淘汰与否仍由类别决定（T/W 本身就是"该删"的声明），
                # 但要把"没说清"这件事报出来——否则契约缺口永远没人补。
                print(f"  提示 {m['src']}/{m['fam']}：re_fetchable 未声明——"
                      f"按类别 {ct.cls} 处理，建议补契约")
            targets.append((m["src"], m["fam"], ct, m["n"]))

        if not targets:
            print("没有可淘汰的族（P/F 与无契约族一律不碰）")
            return 0

        print(f"待评估 {len(targets)} 个族（仅 T/W）：")
        plan = []
        total = 0
        with eng.connect() as c:
            for src, fam, ct, total_rows in targets:
                cutoff = None
                if ct.cls == "W":
                    if not ct.window_days:
                        print(f"  跳过 {src}/{fam}：W 类却没有窗口天数——契约不完整，不猜")
                        continue
                    cutoff = c.execute(text(
                        "SELECT to_char(now() - (:d || ' days')::interval, 'YYYY-MM-DD')"
                    ), {"d": str(ct.window_days)}).scalar()
                sql = _victims_sql(ct.cls)
                params = {"src": src, "fam": fam}
                if cutoff:
                    params["cutoff"] = cutoff
                cnt = c.execute(text(f"SELECT count(*) FROM ({sql}) v"), params).scalar()
                if not cnt:
                    continue
                total += cnt
                plan.append((src, fam, ct.cls, cnt, total_rows, cutoff))
                print(f"  {src}/{fam:<16} {ct.cls}  可搬 {cnt:>9,} / 共 {total_rows:>9,} 行"
                      f"  ≈ {cnt * AVG_ROW_BYTES / 1024 / 1024:>7.1f}MB"
                      + (f"  窗口下界 {cutoff}" if cutoff else "  只留每实体最新一点"))

        if not plan:
            print("没有需要淘汰的行")
            return 0

        print(f"\n合计可搬 {total:,} 行 ≈ {total * AVG_ROW_BYTES / 1024 / 1024:.1f}MB")
        print("目标表 semantic_observations_quarantine（先搬后删，可逆）")

        if not a.apply:
            print("\n（dry-run：未改动任何行。加 --apply 执行搬运。）")
            return 1

        moved_total = 0
        with eng.begin() as c:
            # 4.2 行数不变式：这个工具是本仓唯一会**减少** semantic_observations 的地方，
            # 动手前后必须能对上账——观测减多少、隔离就该加多少，修订表一行不动。
            # 对不上就抛，事务整体回滚（宁可这次白跑，也不留半搬的库）。
            before = _row_counts(c)

            for src, fam, cls, cnt, total_rows, cutoff in plan:
                batch_id = f"retention-{cls}-{src}-{fam}"
                sql = _victims_sql(cls)
                params = {"src": src, "fam": fam}
                if cutoff:
                    params["cutoff"] = cutoff
                remaining = cnt if not a.limit_rows else min(cnt, a.limit_rows)
                moved = 0
                while remaining > 0:
                    take = min(a.batch_size, remaining)
                    rows = c.execute(text(f"""
                        INSERT INTO semantic_observations_quarantine
                          (id, concept_id, entity_type, entity_id, date, granularity,
                           value, unit, source_used, fetched_at, obs_month,
                           declared_type, entity_actual_type, reason, batch_id, quarantined_at)
                        SELECT o.id, o.concept_id, o.entity_type, o.entity_id, o.date,
                               o.granularity, o.value, o.unit, o.source_used, o.fetched_at,
                               o.obs_month, o.entity_type, o.entity_type,
                               :reason, :batch, now()
                          FROM semantic_observations o
                         WHERE o.id IN (SELECT id FROM ({sql}) v LIMIT :n)
                        ON CONFLICT (id) DO NOTHING
                        RETURNING id
                    """), {**params, "n": take, "reason": f"retention_window_evicted:{cls}",
                           "batch": batch_id}).all()
                    ids = [r[0] for r in rows]
                    if not ids:
                        break      # 全冲突或已搬完
                    params_del = {f"id{i}": v for i, v in enumerate(ids)}
                    placeholders = ",".join(f":id{i}" for i in range(len(ids)))
                    c.execute(text(
                        f"DELETE FROM semantic_observations WHERE id IN ({placeholders})"),
                        params_del)
                    moved += len(ids)
                    remaining -= len(ids)
                moved_total += moved
                print(f"  已搬 {src}/{fam}: {moved:,} 行（batch_id={batch_id}）")

            after = _row_counts(c)
            _assert_balanced(before, after, moved_total)   # 对不上就抛，事务回滚

        print(f"\n已搬 {moved_total:,} 行 ≈ {moved_total * AVG_ROW_BYTES / 1024 / 1024:.1f}MB")
        print("回滚（按 batch_id 反向搬回，逐条执行）：")
        for src, fam, cls, *_ in plan:
            print(f"  -- batch_id = retention-{cls}-{src}-{fam}")
            print("  INSERT INTO semantic_observations (" + _LOAD_COLS + ")")
            print("  SELECT " + _LOAD_COLS + " FROM semantic_observations_quarantine")
            print(f"   WHERE batch_id = 'retention-{cls}-{src}-{fam}';")
            print("  DELETE FROM semantic_observations_quarantine")
            print(f"   WHERE batch_id = 'retention-{cls}-{src}-{fam}';")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    finally:
        eng.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
