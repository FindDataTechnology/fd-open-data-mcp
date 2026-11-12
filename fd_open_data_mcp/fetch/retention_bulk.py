"""批量直写脚本的留存收尾（T 只留当期 / W 只留窗口内）。

为什么要有这个模块
------------------
`scripts/bulk_ingest_*.py` 用 ``execute_values`` **直接**写 ``semantic_observations``，
完全绕开 ``fetch.cache`` 的三条写路径。于是契约里写着的 T（只留当期）与 W（滚动窗口）
在这些族上从不生效。

当初的库内指纹是 ``obs_month IS NULL``（``nav`` 1,236 行中 1,227 行为 NULL、
``tenure_days`` 4,280 行全为 NULL）：cache 路径一定会填，不填的必然是另一条写入栈。
**这个指纹已经不能用了**——alembic 0022 把 ``obs_month`` 的派生变成库内触发器
（``trg_sem_obs_month``），任何写者都补得上。现在判断"某族走没走契约"只能看
留存本身：``audit_retention_contracts.py`` 的 ``T_class_accruing_history`` 判据，
以及本模块的静态闸（``tests/test_bulk_retention_guard.py``）。

更麻烦的一层：爬虫集群镜像（``clusters.image``）从 PyPI 装
``fd-open-data-mcp``，而留存语义所在版本当时还没发布。**留存语义只在写入进程所用
镜像里生效**——同库多写者时，旧镜像的写者绕过一切守卫。所以这里的收尾不是"补丁"，
它是那些进程唯一能执行契约的地方。

用法（在同一个事务里，写完立刻收尾）
------------------------------------
    with engine.begin() as conn:
        ...execute_values(...)
        apply_bulk_retention(conn, source=SOURCE, concept_ids=[c["id"] for c in concepts])

``check_bulk_contracts`` 要在**写之前**调（enforce 档缺契约就别写了）。

返回 ``{"t_deleted": n, "w_deleted": m}``；每次 T 合并写一条
``revision_kind='batch'`` 的修订行（``batch_id='bulk-consolidate-<hex>'``），
与被并掉的行数对得上——删了什么都不留痕是这套系统明令禁止的。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from sqlalchemy import bindparam, text

logger = logging.getLogger(__name__)

#: 一次 DELETE 的 id 上限，避免超长 IN 列表。
_DELETE_CHUNK = 500


def _ids(concept_ids: Iterable[int]) -> list[int]:
    return sorted({int(i) for i in concept_ids if i is not None})


def _family_of(code: Optional[str]) -> Optional[str]:
    return str(code).split(".", 1)[0] if code else None


def resolve_bulk_contracts(conn, *, source: str, concept_ids: Iterable[int]) -> dict:
    """``concept_id -> {"family":…, "contract": row|None}``（含 source 级 ``*`` 回退）。"""
    ids = _ids(concept_ids)
    if not ids:
        return {}

    concepts = conn.execute(
        text("SELECT id, code FROM concepts WHERE id IN :ids")
        .bindparams(bindparam("ids", expanding=True)),
        {"ids": ids},
    ).all()
    family_of = {r[0]: _family_of(r[1]) for r in concepts}

    contracts = conn.execute(
        text("""
            SELECT source, family, retention_class, retention_window_days, re_fetchable
              FROM data_retention_contracts
             WHERE source = :src
        """),
        {"src": source},
    ).all()
    by_key = {(r[0], r[1]): r for r in contracts}

    out = {}
    for cid in ids:
        fam = family_of.get(cid)
        ct = by_key.get((source, fam)) or by_key.get((source, "*"))
        out[cid] = {"family": fam, "contract": ct}
    return out


def check_bulk_contracts(conn, *, source: str, concept_ids: Iterable[int]) -> None:
    """写之前检查契约。``enforce`` 档缺契约即抛，其余档只报警。

    与 ``fetch.retention.check_write_allowed`` 同一套档位与同一个异常类型——直写脚本
    不该有自己的一套"更宽松"的闸门。
    """
    from fd_open_data_mcp.fetch.retention import (
        RetentionContractMissing,
        gate_mode,
    )

    mode = gate_mode()
    if mode == "off":
        return
    resolved = resolve_bulk_contracts(conn, source=source, concept_ids=concept_ids)
    missing = [f"{cid}:{v['family']}" for cid, v in resolved.items()
               if v["contract"] is None or not v["contract"].retention_class]
    if not missing:
        return
    detail = f"source={source!r} 缺契约的概念 {len(missing)} 个（{', '.join(missing[:6])}…）"
    if mode == "enforce":
        raise RetentionContractMissing(
            "拒绝批量写入：" + detail + "。先在 data_retention_contracts 声明契约。",
            source=source, family=None,
        )
    logger.warning("留存契约缺失（warn 档，仍写入）：%s", detail)


def _t_victims(conn, source: str, ids: list[int]) -> list:
    """T 类：同一 (概念, 实体, 粒度, 源) 里比最新一条更旧的行。"""
    rows = conn.execute(
        text("""
            SELECT o.id, o.concept_id, o.entity_type, o.entity_id, o.granularity, o.date
              FROM semantic_observations o
             WHERE o.source_used = :src
               AND o.concept_id IN :ids
               AND EXISTS (
                     SELECT 1 FROM semantic_observations n
                      WHERE n.source_used = o.source_used
                        AND n.concept_id = o.concept_id
                        AND n.entity_type = o.entity_type
                        AND n.entity_id = o.entity_id
                        AND n.granularity = o.granularity
                        AND n.date > o.date)
        """).bindparams(bindparam("ids", expanding=True)),
        {"src": source, "ids": ids},
    ).all()
    return rows


def _record_batch_revision(conn, *, groups: dict, source: str, reason: str) -> None:
    """每个被合并的实体会话一条 batch 修订：范围 + 点数 + 原因。"""
    now = datetime.now(timezone.utc)
    for (_cid, _et, _eid, _gran), dates in groups.items():
        conn.execute(
            text("""
                INSERT INTO observation_revisions
                  (concept_id, entity_type, entity_id, date, granularity, source_used,
                   revision_kind, batch_id, range_start, range_end, point_count,
                   reason, revised_at)
                VALUES
                  (:cid, :et, :eid, :date, :gran, :src,
                   'batch', :batch, :r0, :r1, :n, :reason, :now)
            """),
            {"cid": _cid, "et": _et, "eid": _eid, "date": max(dates),
             "gran": _gran, "src": source,
             "batch": f"bulk-consolidate-{uuid.uuid4().hex[:12]}",
             "r0": min(dates), "r1": max(dates), "n": len(dates),
             "reason": reason, "now": now},
        )


def apply_bulk_retention(conn, *, source: str, concept_ids: Iterable[int],
                         reason: str = "bulk_ingest") -> dict:
    """写完之后的收尾：W 删窗口外、T 只留每实体最新一条（带 batch 修订）。

    **必须与写入在同一事务里**：先提交再收尾的话，中途失败会留下"写了没裁"的状态，
    与它要修的那个问题一模一样。
    """
    ids = _ids(concept_ids)
    if not ids:
        return {"t_deleted": 0, "w_deleted": 0}

    resolved = resolve_bulk_contracts(conn, source=source, concept_ids=ids)
    t_ids, w_cutoffs = [], {}
    for cid, info in resolved.items():
        ct = info["contract"]
        if ct is None or not ct.retention_class:
            continue                      # 无契约：不猜（"不知道"不等于"可以删"）
        if ct.retention_class == "T":
            t_ids.append(cid)
        elif ct.retention_class == "W" and ct.retention_window_days:
            cutoff = (datetime.now(timezone.utc).date()
                      - timedelta(days=int(ct.retention_window_days))).isoformat()
            w_cutoffs[cid] = cutoff

    w_deleted = 0
    for cid, cutoff in w_cutoffs.items():
        # 只比完整的 ISO 日期：裸年/年月没有唯一方向，猜一个就是在替上游决定口径。
        res = conn.execute(
            text("""
                DELETE FROM semantic_observations
                 WHERE source_used = :src AND concept_id = :cid AND date < :cutoff
                   AND length(date) = 10 AND substr(date, 5, 1) = '-' AND substr(date, 8, 1) = '-'
            """),
            {"src": source, "cid": cid, "cutoff": cutoff},
        )
        w_deleted += res.rowcount or 0

    t_deleted = 0
    if t_ids:
        victims = _t_victims(conn, source, t_ids)
        if victims:
            groups: dict = {}
            for _id, cid, et, eid, gran, d in victims:
                groups.setdefault((cid, et, eid, gran), []).append(d)
            _record_batch_revision(conn, groups=groups, source=source, reason=reason)
            victim_ids = [r[0] for r in victims]
            for i in range(0, len(victim_ids), _DELETE_CHUNK):
                chunk = victim_ids[i:i + _DELETE_CHUNK]
                res = conn.execute(
                    text("DELETE FROM semantic_observations WHERE id IN :ids")
                    .bindparams(bindparam("ids", expanding=True)),
                    {"ids": chunk},
                )
                t_deleted += res.rowcount or 0

    if t_deleted or w_deleted:
        logger.info(
            "批量留存收尾：T 合并 %d 行（%d 个概念）、W 窗口淘汰 %d 行（source=%s）",
            t_deleted, len(t_ids), w_deleted, source)
    return {"t_deleted": t_deleted, "w_deleted": w_deleted}
