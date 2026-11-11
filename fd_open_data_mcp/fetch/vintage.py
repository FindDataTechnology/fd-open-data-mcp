"""按历史时点读取观测（vintage read）。

历史已经在 `observation_revisions` 里了，但此前除测试外无人读取——「当时这个数是
多少」回答不了。本模块把它变成可用的读路径。

三种回放状态（必须可区分，不得合并成「无数据」）：

``exact``
    该点的历史值可精确重建。单点覆写（``revision_kind='overwrite'``）保留了
    ``old_value``，可以沿修订链反向还原到任意时点。

``batch_unreplayable``
    该点落在一次整段修订（``'batch'``，如前复权后整条序列重写）覆盖的区间内。batch
    只记区间与点数，逐点旧值按设计不存，所以**逐点历史永久不可还原**。此时不得拿当前
    值冒充当时的值，只能显式说明。

``vintage_unavailable``
   该点在请求时点之前没有任何可判定依据：既无更早的修订记录，也无可信的写入时点。
   早于修订留痕启用（迁移 0018）的区间典型属这一类。

另有 ``absent``：该点在请求时点尚不存在（首次写入晚于请求时点）。这不是回放失败，
是「那时还不知道」，与「无数据」同形但语义不同，故单独命名。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional, Sequence

EXACT = "exact"
BATCH_UNREPLAYABLE = "batch_unreplayable"
VINTAGE_UNAVAILABLE = "vintage_unavailable"
ABSENT = "absent"


@dataclass
class Revision:
    """回放所需的修订最小信息（与 ORM 行解耦，便于单测）。"""

    revised_at: datetime
    old_value: Optional[str]
    new_value: Optional[str]
    revision_kind: str = "overwrite"
    batch_id: Optional[str] = None
    range_start: Optional[str] = None
    range_end: Optional[str] = None
    revision_id: Optional[int] = None


@dataclass
class ReplayResult:
    value: Optional[str]
    status: str
    detail: Optional[str] = None
    batch_id: Optional[str] = None
    reparsed_from: int = 0


def normalize_as_of(as_of_vintage) -> Optional[datetime]:
    """把 ``as_of_vintage`` 规整成 naive UTC datetime（与库内 naive UTC 同口径）。

    接受 ``date`` / ``datetime`` / ``'YYYY-MM-DD'`` / ISO-8601 字符串。裸日期按**当日
    结束**处理：问"2024-12-31 那天我们知道什么"时，当天写入的修订应当可见。
    """
    if as_of_vintage is None:
        return None
    if isinstance(as_of_vintage, datetime):
        dt = as_of_vintage
    elif isinstance(as_of_vintage, str):
        raw = as_of_vintage.strip()
        try:
            if len(raw) == 10:
                dt = datetime.fromisoformat(raw + "T23:59:59")
            else:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"invalid as_of_vintage={as_of_vintage!r}") from exc
    else:
        raise ValueError(f"invalid as_of_vintage={as_of_vintage!r}")
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _naive_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def _covered_by_batch(
    obs_date: str, granularity: Optional[str], revisions: Sequence[Revision], as_of: datetime
) -> Optional[Revision]:
    """该点是否落在某次「≤ as_of 的」整段修订覆盖区间内？返回那次修订（若有）。"""
    for rev in revisions:
        if rev.revision_kind != "batch":
            continue
        if rev.revised_at > as_of:
            continue
        lo, hi = rev.range_start, rev.range_end
        if lo is None or hi is None:
            continue
        if lo <= obs_date <= hi:
            return rev
    return None


def replay_point(
    obs_date: str,
    granularity: Optional[str],
    current_value: Optional[str],
    fetched_at: Optional[datetime],
    revisions: Iterable[Revision],
    as_of_vintage,
) -> ReplayResult:
    """把单个观测点回放到 ``as_of_vintage`` 时点的值。

    重放规则：从当前值出发，把「晚于 as_of 的修订」逐条倒推回其 ``old_value``；等价
    但更稳的写法是沿时间正序取「最后一条 ≤ as_of 的修订」的 ``new_value``——本实现用
    后者，因为它不依赖当前值仍等于修订链末端（当前值可能已被其它路径改动）。

    单点覆写之外的情况一律**显式说明原因**，绝不用当前值冒充历史值。
    """
    as_of = normalize_as_of(as_of_vintage)
    if as_of is None:
        return ReplayResult(current_value, EXACT, detail="no as_of requested")

    revs = sorted(
        revisions,
        key=lambda r: (_naive_utc(r.revised_at), r.revision_id or 0),
    )
    overwrites = [r for r in revs if r.revision_kind != "batch"]

    batch = _covered_by_batch(obs_date, granularity, revs, as_of)
    if batch is not None:
        return ReplayResult(
            None, BATCH_UNREPLAYABLE,
            detail=(f"整段修订覆盖该点（{batch.range_start}..{batch.range_end}），"
                    f"逐点旧值未留存"),
            batch_id=batch.batch_id,
        )

    applied = [r for r in overwrites if _naive_utc(r.revised_at) <= as_of]
    if applied:
        latest = applied[-1]
        if latest.new_value is None:
            return ReplayResult(None, ABSENT, detail="该时点的值为空（来源未给值）")
        return ReplayResult(latest.new_value, EXACT, reparsed_from=len(applied))

    # 没有任何 ≤ as_of 的修订：说明该时点早于所有已知修订。
    if overwrites:
        earliest = overwrites[0]
        if earliest.old_value is not None:
            # 第一次修订发生在 as_of 之后 → 当时的值就是那次被替换掉的旧值。
            return ReplayResult(earliest.old_value, EXACT, reparsed_from=1)
        return ReplayResult(
            None, VINTAGE_UNAVAILABLE,
            detail="该点早于首条可追溯修订，且旧值未留存",
        )

    # 从未被修订过：写入时点早于 as_of 即可视作当时已存在。
    if fetched_at is None:
        return ReplayResult(
            None, VINTAGE_UNAVAILABLE, detail="无修订记录且无写入时点，无法判定"
        )
    if _naive_utc(fetched_at) <= as_of:
        return ReplayResult(current_value, EXACT)
    return ReplayResult(
        None, ABSENT, detail=f"首次写入晚于请求时点（fetched_at={_naive_utc(fetched_at)}）"
    )


def summarize(results: Iterable[ReplayResult]) -> dict:
    """把逐点结果汇总成一句可对外声明的口径。"""
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    total = sum(counts.values())
    return {
        "points": total,
        "exact": counts.get(EXACT, 0),
        "batch_unreplayable": counts.get(BATCH_UNREPLAYABLE, 0),
        "vintage_unavailable": counts.get(VINTAGE_UNAVAILABLE, 0),
        "absent": counts.get(ABSENT, 0),
        # 一句话结论：有任何一点不可精确回放，就不许说"完整可回放"。
        "fully_exact": total == counts.get(EXACT, 0),
    }


def load_revisions(
    session, concept_id: int, entity_type: str, entity_id: int,
    start: str, end: str, as_of: Optional[datetime],
) -> tuple[dict[tuple[str, str, str], list[Revision]],
           dict[tuple[str, str], list[Revision]]]:
    """取该 (concept, entity) 在窗口内的修订。

    返回两组，因为两种修订的坐标语义不同：

    * ``by_point``：单点覆写按 ``(date, granularity, source)`` 归组，只作用于该点。
    * ``by_gran_source``：整段修订（batch）只在区间起点记一行，却影响区间内**每一个**
      点。按点坐标归组会让区间内其余点全部漏掉这次修订——于是"不可回放"被误判成
      "可精确回放"，拿当前值冒充当年值。

    **不按 `revised_at` 预过滤**——这是本模块最容易写错的一处。若在 SQL 层只取
    ``revised_at <= as_of`` 的修订，那么"该点的第一条修订发生在请求时点之后"这种
    情形里，重放就看不到那条修订，只能退回 `fetched_at` 判断，从而把一个**已知**
    的值误报成 `absent`（"当时还不知道"）。而按语义，请求时点的值恰恰是那条修订被
    替换掉的 `old_value`。故此函数取全部修订，由 `replay_point` 自行切分
    "≤ as_of 已生效"与"首条更晚的修订"。
    """
    from sqlalchemy import or_
    from fd_open_data_mcp.models import ObservationRevision

    q = session.query(ObservationRevision).filter(
        ObservationRevision.concept_id == concept_id,
        ObservationRevision.entity_type == entity_type,
        ObservationRevision.entity_id == entity_id,
        or_(
            ObservationRevision.date.between(start, end),
            (ObservationRevision.revision_kind == "batch")
            & (ObservationRevision.range_start <= end)
            & (ObservationRevision.range_end >= start),
        ),
    )
    by_point: dict[tuple[str, str, str], list[Revision]] = {}
    by_gran_source: dict[tuple[str, str], list[Revision]] = {}
    for r in q.all():
        rev = Revision(
            revised_at=r.revised_at, old_value=r.old_value, new_value=r.new_value,
            revision_kind=r.revision_kind, batch_id=r.batch_id,
            range_start=r.range_start, range_end=r.range_end, revision_id=r.id,
        )
        if r.revision_kind == "batch":
            by_gran_source.setdefault(
                (r.granularity or "day", r.source_used), []
            ).append(rev)
        else:
            by_point.setdefault(
                (r.date, r.granularity or "day", r.source_used), []
            ).append(rev)
    return by_point, by_gran_source


def read_range_as_of(
    session, concept_id: int, entity_type: str, entity_id: int,
    start: str, end: str, as_of_vintage,
) -> dict:
    """按历史时点读一段序列。

    返回 ``values``（与普通读同形，额外带 ``vintage_status``）与 ``vintage`` 汇总。
    不可精确回放的点**不返回数值**（``value`` 为 None），避免下游把当前值当成当年值。
    """
    from fd_open_data_mcp.fetch.cache import read_cache_range

    as_of = normalize_as_of(as_of_vintage)
    rows = read_cache_range(session, concept_id, entity_type, entity_id, start, end)
    by_point, by_gran_source = load_revisions(
        session, concept_id, entity_type, entity_id, start, end, as_of)

    ordered = sorted(rows, key=lambda r: r.date)
    values: list[dict] = []
    results: list[ReplayResult] = []
    details: list[dict] = []

    # 粒度不一致守卫：同一 (date, source) 有修订、但粒度与观测行不同 —— 例如观测
    # 以 ``day`` 写入而修订记在 ``year`` 上。此时键对不上，重放会取不到修订，进而
    # 退回 ``fetched_at`` 判成 ``absent``。而 ``absent`` 是一句**断言**（"当时还没这条
    # 数据"）；粒度错配时我们其实**不知道**当时有没有 —— 那正是
    # ``vintage_unavailable`` 的定义。不加这条守卫，错配会被报成一句假话。
    rev_granularities: dict[tuple[str, str], set[str]] = {}
    for (d, g, s) in by_point:
        rev_granularities.setdefault((d, s), set()).add(g)

    for row in ordered:
        gran = getattr(row, "granularity", None) or "day"
        key = (row.date, gran, row.source_used)
        revs = list(by_point.get(key, [])) + list(by_gran_source.get((gran, row.source_used), []))
        if not by_point.get(key):
            other = rev_granularities.get((row.date, row.source_used), set())
            if other and gran not in other:
                res = ReplayResult(
                    None, VINTAGE_UNAVAILABLE,
                    detail=(f"修订记录的粒度（{sorted(other)}）与观测行（{gran}）不一致，"
                            "无法判定该时点的值"),
                )
                results.append(res)
                details.append({"date": row.date, "status": res.status,
                                "detail": res.detail, "batch_id": None})
                continue
        res = replay_point(
            row.date, gran, row.value, row.fetched_at, revs, as_of,
        )
        results.append(res)
        if res.value is None:
            details.append({"date": row.date, "status": res.status,
                            "detail": res.detail, "batch_id": res.batch_id})
            continue
        values.append({
            "date": row.date,
            "value": res.value,
            "unit": row.unit,
            "source_used": row.source_used,
            "vintage_status": res.status,
        })

    # 被留存策略淘汰的点：它们不在 `rows` 里（已搬进隔离表），若不单独查，
    # 回放结果会把"曾经有、按契约删了"与"从来没有"混成同一件事。
    evicted = load_evicted_points(
        session, concept_id, entity_type, entity_id, start, end)
    if evicted:
        # 去重要用「库里现存的点」，不是 `details`——details 只装回放失败的点，
        # 现存的点大多能回放、不会出现在里面。
        present = {row.date for row in ordered}
        for d, info in sorted(evicted.items()):
            if d in present:
                continue      # 现存行优先：还在库里的点按正常回放判
            window = retention_window_note(session, concept_id,
                                           info.get("source_used") or "")
            res = ReplayResult(
                None, VINTAGE_UNAVAILABLE,
                detail=("该点曾被覆盖，后按留存策略淘汰（"
                        f"批次 {info.get('batch_id')}，{info.get('reason') or 'retention'}）"
                        + (f"；{window}" if window else "")
                        + "；不再返回数值，且不等同于「从未覆盖」"),
                batch_id=info.get("batch_id"),
            )
            results.append(res)
            details.append({"date": d, "status": res.status,
                            "detail": res.detail, "batch_id": res.batch_id,
                            "evicted": True})

    summary = summarize(results)
    summary["skipped"] = len(results) - len(values)
    summary["evicted_by_retention"] = sum(
        1 for x in details if x.get("evicted"))
    return {
        "as_of_vintage": as_of.isoformat() if as_of else None,
        "values": values,
        "count": len(values),
        "vintage": summary,
        "details": details,
    }


EVICTED = "evicted_by_retention"


def load_evicted_points(
    session, concept_id: int, entity_type: str, entity_id: int,
    start: str, end: str,
) -> dict[str, dict]:
    """取「曾被覆盖、后来按留存契约淘汰」的点（从隔离表读）。

    为什么必须单独查：读路径只看现存行，被淘汰的点**根本不在结果里**——于是
    "曾经有数据，按契约删了"与"从来没有覆盖过"在返回体里长得一模一样，下游无从区分。
    `establish-data-retention-principle` R3 要求这两件事必须是两种返回体。

    表不存在时返回空（单测的 sqlite 环境没有这张表），不抛——"查不到"不等于
    "可以报 absent"，但这里连"曾淘汰"的依据都没有，只能让调用方按原样处理。
    """
    from sqlalchemy import text

    sql = text("""
        SELECT q.date, q.source_used, q.batch_id, q.reason, q.quarantined_at
          FROM semantic_observations_quarantine q
         WHERE q.concept_id = :cid
           AND q.entity_type = :et
           AND q.entity_id = :eid
           AND q.date BETWEEN :start AND :end
           AND q.batch_id IS NOT NULL
    """)
    try:
        rows = session.execute(sql, {
            "cid": concept_id, "et": entity_type, "eid": entity_id,
            "start": start, "end": end,
        }).all()
    except Exception:  # noqa: BLE001 - 隔离表可能在当前库不存在
        return {}
    out: dict[str, dict] = {}
    for r in rows:
        out.setdefault(str(r[0]), {
            "source_used": r[1], "batch_id": r[2], "reason": r[3],
            "quarantined_at": r[4],
        })
    return out


def retention_window_note(session, concept_id: int, source_used: str) -> Optional[str]:
    """该 (概念, 源) 的留存窗口，用于在返回体里说明"为什么这个点不在"。"""
    try:
        from fd_open_data_mcp.fetch.retention import family_of, resolve_contract
        from fd_open_data_mcp.models import Concept

        concept = session.get(Concept, concept_id)
        if concept is None:
            return None
        contract = resolve_contract(session, source_used, family_of(concept.code))
        if contract is None or not contract.retention_window_days:
            return None
        return f"该源留存策略 {contract.retention_class}/{contract.retention_window_days} 天"
    except Exception:  # noqa: BLE001
        return None
