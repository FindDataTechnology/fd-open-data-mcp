#!/usr/bin/env python3
"""价格口径一致性探针（openspec change: add-price-supply-and-caliber, tasks 2.1-2.4）。

只读：只发 SELECT，不建表、不写行、不落缓存、不写报告文件。全部输出走标准输出。
退出码：0 = 零违规；1 = 有违规；2 = 跑不起来（库不可达 / 声明不可加载）。

三条判据（缺一不可，且都必须**现在能红**）：

1. 三路一致——研究取价 / 交易行情 / 结算取价对同一价格族必须指向同一个 ``source_id``，
   且读到同一个水位。任一未登记即违规（"没有配置"不是"一致"）。
2. 基准可得——指数族必须存在覆盖窗口的观测。零观测是违规，不是"暂时没有"。
3. 跨源未超阈——同一 ``(标的, 日期)`` 在两个已登记源之间的价差不得超阈值。
   超了就是口径分歧，必须报出来，不许择一继续。

用法：

    python scripts/price_caliber_gate.py
    python scripts/price_caliber_gate.py --json
    python scripts/price_caliber_gate.py --trading astock_daily --research akshare --settle astock_daily

判据一默认从环境变量取三路，取不到就是"未配置"——这本身就是一条违规，因为
"三路里有一路没说清楚用哪个源"正是这个 change 要消灭的状态。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("price_caliber_gate")
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_THRESHOLD_PCT = 0.5  # 实测两源同日整差 1.00 元 / 约 36 元 ≈ 2.7%，0.5% 足以红

#: 三路各自从哪个环境变量读 source_id。交易路在生产清单里是 MARKET_DATA_PROVIDER
#: （本次 change 后将改为 MARKET_DATA_SOURCE_ID），旧名一并认，避免探针在迁移期误报。
PATH_ENV = {
    "trading": ("MARKET_DATA_SOURCE_ID", "MARKET_DATA_PROVIDER", "ASTOCK_SOURCE"),
    "research": ("RESEARCH_PRICE_SOURCE", "DAAS_PRICE_SOURCE"),
    "settle": ("SETTLE_PRICE_SOURCE", "DECISION_SETTLE_PRICE_SOURCE"),
}


@dataclass
class Violation:
    code: str
    judgment: str
    detail: str
    fix: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def line(self) -> str:
        head = f"[{self.judgment}] {self.code}: {self.detail}"
        return f"{head}\n    修法: {self.fix}"


@dataclass
class Report:
    violations: list[Violation] = field(default_factory=list)
    checked: dict[str, Any] = field(default_factory=dict)

    def add(self, v: Violation) -> None:
        self.violations.append(v)

    @property
    def ok(self) -> bool:
        return not self.violations

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "violation_count": len(self.violations),
            "violations": [
                {
                    "code": v.code,
                    "judgment": v.judgment,
                    "detail": v.detail,
                    "fix": v.fix,
                    "evidence": v.evidence,
                }
                for v in self.violations
            ],
            "checked": self.checked,
        }


def _env_source(names: Sequence[str]) -> str | None:
    for n in names:
        v = os.environ.get(n)
        if v:
            return v.strip()
    return None


# ---------------------------------------------------------------- 判据一


def _shard_watermark(decl, today: dt.date) -> "tuple[str | None, str | None]":
    """注册表水位不可得时，直接从**该源自己的库**问最后一根 bar 是哪天。

    ## 为什么必须有这一步

    `derive_watermarks` 查的是**本体库**（get_database() → fd_open_data）。而
    `access.kind=federated_view` 的源，价格行在**分片库**里：本体库自 2025-10-06
    起没有价格观测。生产实测 `astock_daily` 的注册表水位恒为 `no_data`，
    于是判据一永远报 `WATERMARK_UNAVAILABLE`——而分片里明明有到 2026-11-09 的 bar。

    这就是「读不到 ≠ 不存在」：把"我查的库没有"当成"源没有数据"，
    会让一条本该绿的判据长期红着，最后没人再看它。

    返回 ``(as_of, method)``；问不到返回 ``(None, None)``，**不编造**。
    """
    access = getattr(decl, "access", None)
    dsn_env = getattr(access, "dsn_env", None) if access is not None else None
    kind = getattr(access, "kind", None) if access is not None else None
    if kind != "federated_view" or not dsn_env:
        return None, None
    dsn = os.environ.get(dsn_env, "")
    if not dsn:
        return None, None

    probe = getattr(decl, "watermark_probe", None)
    codes = list(getattr(probe, "concept_codes", ()) or ())
    basket = list(getattr(probe, "reference_basket", ()) or ())
    etype = getattr(probe, "entity_type", None)
    db_source = getattr(decl, "db_source", None) or getattr(decl, "source_id", None)

    try:
        import psycopg2

        with psycopg2.connect(dsn, connect_timeout=15) as conn:
            with conn.cursor() as cur:
                # 与 derive_watermark 同一组条件，只换库：源 + 实体类型 + 概念码。
                # 不加参照篮（篮是本体库的 entity_id，分片里未必同 id）。
                # **按 entity+concept 定位，不按 source_used 扫全表**。实测：
                # `where source_used = …` 单条件在这张视图上 25s 都跑不完（无该列索引），
                # 而 (entity_id, concept_id) 有索引、1.5s 内出结果。
                # 另外 `date` 列是 varchar（既有事实），所以只取 max(date) 字符串比较，
                # 不拿它跟 timestamptz 比——那会直接类型报错。
                row = None
                for code in codes:
                    cur.execute(
                        "select o.concept_id from semantic_observations_all o "
                        "join concepts c on c.id = o.concept_id "
                        "where c.code = %s and c.entity_type = %s "
                        "and o.source_used = %s "
                        "and o.entity_id = (select id from entities where code = %s limit 1) "
                        "limit 1",
                        (code, etype, db_source, basket[0] if basket else "600036"),
                    )
                    hit = cur.fetchone()
                    if not hit:
                        continue
                    cur.execute(
                        "select max(date) from semantic_observations_all "
                        "where entity_id = (select id from entities where code = %s limit 1) "
                        "and concept_id = %s and source_used = %s",
                        (basket[0] if basket else "600036", hit[0], db_source),
                    )
                    row = cur.fetchone()
                    if row and row[0]:
                        break
    except Exception:  # noqa: BLE001 - 探针只读，连不上就是"问不到"
        logger.warning("shard watermark probe failed for %s", db_source, exc_info=True)
        return None, None

    if not row or row[0] is None:
        return None, None
    as_of = row[0].isoformat() if hasattr(row[0], "isoformat") else str(row[0])[:10]
    return as_of, "provider_max_date"

def judge_three_paths(reg, session, paths: dict[str, str | None], today: dt.date) -> tuple[list[Violation], dict]:
    from fd_open_data_mcp.price_sources.watermark import derive_watermarks

    out: list[Violation] = []
    resolved: dict[str, dict] = {}
    decls: dict[str, Any] = {}

    for path, sid in paths.items():
        if not sid:
            out.append(
                Violation(
                    code="PATH_SOURCE_UNSPECIFIED",
                    judgment="three_paths",
                    detail=f"{path} 路没有声明价格源（环境变量 {', '.join(PATH_ENV.get(path, ()))} 均未设置）",
                    fix=(
                        "为该路显式指定已登记的 source_id；研究/结算若仍走 DAAS 通道 B，"
                        "须把该通道的源登记进 declarations.yaml 后再声明"
                    ),
                    evidence={"path": path},
                )
            )
            resolved[path] = {"source_id": None}
            continue
        try:
            decl = reg.get(sid)
        except Exception as exc:
            out.append(
                Violation(
                    code="PATH_SOURCE_UNREGISTERED",
                    judgment="three_paths",
                    detail=f"{path} 路指向未登记的源 {sid!r}: {exc}",
                    fix="在 declarations.yaml 中登记该源，或把该路改为已登记的 source_id",
                    evidence={"path": path, "source_id": sid},
                )
            )
            resolved[path] = {"source_id": sid, "registered": False}
            continue
        resolved[path] = {"source_id": sid, "registered": True}
        # 声明对象只留在本地（水位派生用），不进输出——探针输出必须是纯 JSON 数据。
        decls[sid] = decl

    marks = derive_watermarks(session, decls, today=today) if decls else {}
    for path, sid in ((p, v.get("source_id")) for p, v in resolved.items()):
        if sid in marks:
            resolved[path]["as_of"] = marks[sid].as_of
            resolved[path]["watermark_status"] = marks[sid].status

    # 本体库给不出 → 问该源自己的库（2026-11-10 生产实测的必要性，见 _shard_watermark）。
    # 来路必须可分：registry 与自证不能长得一样。
    for sid, decl in decls.items():
        mark = marks.get(sid)
        if mark is not None and mark.status == "ok":
            continue
        as_of, method = _shard_watermark(decl, today)
        if not as_of:
            continue
        for path, v in resolved.items():
            if v.get("source_id") == sid:
                v["as_of"] = as_of
                v["watermark_status"] = "ok"
                v["watermark_method"] = method
                v["watermark_registry_status"] = getattr(mark, "status", None)

    ids = {v.get("source_id") for v in resolved.values() if v.get("source_id")}
    if len(ids) > 1:
        out.append(
            Violation(
                code="SOURCE_ID_DIVERGENCE",
                judgment="three_paths",
                detail="三路指向不同的源：" + ", ".join(
                    f"{p}={v.get('source_id')}" for p, v in resolved.items()
                ),
                fix="把三路收敛到同一 source_id（或将差异登记为显式换源事件并重跑历史）",
                evidence={p: v.get("source_id") for p, v in resolved.items()},
            )
        )

    as_ofs = {v.get("as_of") for v in resolved.values() if v.get("as_of")}
    if len(as_ofs) > 1:
        out.append(
            Violation(
                code="AS_OF_DIVERGENCE",
                judgment="three_paths",
                detail="三路读到不同的水位：" + ", ".join(
                    f"{p}={v.get('as_of')}" for p, v in resolved.items()
                ),
                fix="三路必须读同一个水位对象；检查是否有某一路在缓存或自行推导水位",
                evidence={p: v.get("as_of") for p, v in resolved.items()},
            )
        )
    if len(ids) == 1 and any(v.get("watermark_status") not in (None, "ok") for v in resolved.values()):
        out.append(
            Violation(
                code="WATERMARK_UNAVAILABLE",
                judgment="three_paths",
                detail="三路同源但水位不可得（源无数据或查询失败）",
                fix="确认该源在库内有覆盖观测；若源已停更，这是真结论——需要供数决策而非改代码",
                evidence={p: v.get("watermark_status") for p, v in resolved.items()},
            )
        )

    return out, {"paths": resolved}


# ---------------------------------------------------------------- 判据二


def judge_index(session, reg, today: dt.date) -> tuple[list[Violation], dict]:
    from fd_open_data_mcp.models import Concept, SemanticObservation
    from sqlalchemy import func, select

    try:
        decl = reg.resolve_for_family("price.index.daily")
    except Exception as exc:
        return [
            Violation(
                code="INDEX_FAMILY_NO_SOURCE",
                judgment="index_series",
                detail=f"price.index.daily 没有唯一首选源: {exc}",
                fix="在 declarations.yaml 中为 price.index.daily 指定唯一首选源",
            )
        ], {}

    cutoff = (today - dt.timedelta(days=decl.watermark_probe.lookback_days)).isoformat()
    try:
        total = session.execute(
            select(func.count(SemanticObservation.id))
            .select_from(SemanticObservation)
            .join(Concept, Concept.id == SemanticObservation.concept_id)
            .where(
                SemanticObservation.entity_type == "index",
                SemanticObservation.source_used == decl.db_source,
            )
        ).scalar()
        latest = session.execute(
            select(func.max(SemanticObservation.date))
            .select_from(SemanticObservation)
            .join(Concept, Concept.id == SemanticObservation.concept_id)
            .where(
                SemanticObservation.entity_type == "index",
                SemanticObservation.source_used == decl.db_source,
                SemanticObservation.date >= cutoff,
            )
        ).scalar()
    except Exception as exc:
        return [
            Violation(
                code="INDEX_QUERY_FAILED",
                judgment="index_series",
                detail=f"指数族查询失败: {exc}",
                fix="确认库可达；查询失败不等于没有数据，不得据此判定通过",
            )
        ], {}

    checked = {
        "source_id": decl.source_id,
        "db_source": decl.db_source,
        "total_index_observations": total,
        "latest_in_window": latest,
        "window_from": cutoff,
    }
    if not total:
        return [
            Violation(
                code="INDEX_SERIES_ABSENT",
                judgment="index_series",
                detail=(
                    f"指数族观测数为 0（source={decl.db_source}）；基准相对指标"
                    "（超额/跟踪误差/IR/组合 alpha）因此结构性不可得"
                ),
                fix=(
                    "点亮指数日线：一次性种子导入（license_state=seed，对外须声明来源与待替换），"
                    "或接入持牌源后换 source_id"
                ),
                evidence=checked,
            )
        ], checked

    return [], checked


# ---------------------------------------------------------------- 判据三


def _shard_series(decl, codes, basket, since: str) -> "dict[tuple[str, str], float]":
    """从**该源自己的库**取参照篮的 (symbol, date) → value。

    判据三原先只查本体库，而 `federated_view` 源的行在分片库里——生产实测
    本体库 `astock_daily` 是 **0 行**（akshare 有 981 万行），于是"两源无同日重叠"
    成了必然结论：空比对被读成"没法判"，而真相是"我只查了一个库"。

    查询形状与 `_shard_watermark` 一致：按 entity+concept 走索引（该列组合有索引），
    不按 source_used 扫全表（实测无索引、25s 超时）；`date` 是 varchar，只做字符串比较。
    """
    access = getattr(decl, "access", None)
    if getattr(access, "kind", None) != "federated_view":
        return {}
    dsn_env = getattr(access, "dsn_env", None)
    dsn = os.environ.get(dsn_env, "") if dsn_env else ""
    if not dsn:
        return {}
    db_source = getattr(decl, "db_source", None) or getattr(decl, "source_id", None)

    out: dict[tuple[str, str], float] = {}
    try:
        import psycopg2

        with psycopg2.connect(dsn, connect_timeout=10,
                              options="-c statement_timeout=20000") as conn:
            with conn.cursor() as cur:
                for symbol in basket:
                    cur.execute(
                        "select o.concept_id from semantic_observations_all o "
                        "join concepts c on c.id = o.concept_id "
                        "where c.code = %s and c.entity_type = 'stock' "
                        "and o.source_used = %s "
                        "and o.entity_id = (select id from entities where code = %s limit 1) "
                        "limit 1",
                        (codes[0], db_source, symbol),
                    )
                    hit = cur.fetchone()
                    if not hit:
                        continue
                    cur.execute(
                        "select date, value from semantic_observations_all "
                        "where entity_id = (select id from entities where code = %s limit 1) "
                        "and concept_id = %s and source_used = %s and date >= %s",
                        (symbol, hit[0], db_source, since),
                    )
                    for date_s, value in cur.fetchall():
                        try:
                            out[(symbol, str(date_s)[:10])] = float(value)
                        except (TypeError, ValueError):
                            continue
    except Exception:  # noqa: BLE001 - 探针只读，连不上就是"取不到"
        logger.warning("shard series probe failed for %s", db_source, exc_info=True)
        # 抛出去让调用方区分"没读到"与"真无重叠"——这两件事在报告上必须不同形。
        raise
    return out


def judge_divergence(session, reg, today: dt.date, threshold_pct: float) -> tuple[list[Violation], dict]:
    from fd_open_data_mcp.models import Concept, Entity, SemanticObservation
    from sqlalchemy import select

    # 同族（price.stock.daily）下所有已登记源两两比对；只有声明了该族的源才参与。
    try:
        family_owner = reg.resolve_for_family("price.stock.daily").source_id
    except Exception as exc:
        return [
            Violation(
                code="STOCK_FAMILY_NO_SOURCE",
                judgment="cross_source",
                detail=f"price.stock.daily 没有唯一首选源: {exc}",
                fix="在 declarations.yaml 中为 price.stock.daily 指定唯一首选源",
            )
        ], {}

    owner_decl = reg.get(family_owner)
    candidates = [
        d for d in reg.sources.values()
        if "stock" in d.instrument_types and d.source_id != family_owner
    ]
    if not candidates:
        return [], {"note": "only one stock source registered; nothing to cross-check"}

    # ── 只比**同锚点**的源（2026-11-10 生产实测）────────────────────────
    #
    # 两源都声明 qfq，但锚点不同：astock_daily 锚在 "2026-07-10"，akshare 是 per_fetch。
    # 前复权序列的绝对值依赖锚点日，异锚两条序列**本就不可直接比**——实测同一标的
    # 的比值在一周内从 1.00415 漂到 1.02172（不是恒定缩放，是按日期变的），
    # 于是 0.5% 阈值会连报 64 天"分歧"，而那不是数据错，是拿两种口径当一种比。
    #
    # 处置：异锚的源不进比对，单独报一条 `CROSS_SOURCE_ANCHOR_MISMATCH`（点名两侧锚点）。
    # 「不可比」要明说，不能既不算分歧也不算通过。
    owner_anchor = getattr(owner_decl, "adjustment_anchor", None)
    comparable, anchor_mismatch = [], []
    for cand in candidates:
        cand_anchor = getattr(cand, "adjustment_anchor", None)
        same_caliber = (
            getattr(cand, "adjustment", None) == getattr(owner_decl, "adjustment", None)
            and cand_anchor == owner_anchor
        )
        (comparable if same_caliber else anchor_mismatch).append(cand)

    owner = owner_decl
    basket = owner.watermark_probe.reference_basket
    codes = list(owner.watermark_probe.concept_codes)

    rows = session.execute(
        select(
            SemanticObservation.entity_id,
            Entity.code,
            SemanticObservation.date,
            SemanticObservation.source_used,
            SemanticObservation.value,
        )
        .select_from(SemanticObservation)
        .join(Concept, Concept.id == SemanticObservation.concept_id)
        .join(Entity, Entity.id == SemanticObservation.entity_id)
        .where(
            Concept.code.in_(codes),
            Entity.entity_type == "stock",
            Entity.code.in_(list(basket)),
            SemanticObservation.date >= (today - dt.timedelta(days=owner.watermark_probe.lookback_days)).isoformat(),
            # 只取**可比源**的行：异锚源即便本体库里有行，也不能掺进 pivot——
            # 否则"锚点不同不可比"会被下游的价差判定覆盖成一条 CALIBER_DIVERGENCE。
            # 实测（prod 声明）：akshare 锚 per_fetch，会报 600036@2026-07-10 差 2.72%。
            SemanticObservation.source_used.in_(
                [family_owner, *[c.source_id for c in comparable]]
            ),
        )
    ).all()

    # (symbol, date) -> {source: value}
    pivot: dict[tuple[str, str], dict[str, float]] = {}
    for _eid, symbol, date, source, value in rows:
        try:
            fv = float(value)
        except (TypeError, ValueError):
            continue
        pivot.setdefault((symbol, str(date)[:10]), {})[source] = fv

    # 本体库给不出行的源（access.kind=federated_view）→ 从它**自己的库**补齐再比对。
    #
    # 生产实测：本体库里 astock_daily 是 **0 行**、akshare 有 981 万行，于是
    # "两源无同日重叠"成了必然结论——`NO_CROSS_SOURCE_OVERLAP` 被读成"没法判"，
    # 而真相是"我只查了一个库"。空比对不是通过，也不该是一条长期红着的噪音。
    # 跨源比对只需要若干重叠日，不必扫满 lookback 窗：实测 400 天窗会在分片上
    # 撞 20s 语句超时（该视图的 (date) 侧没有可用索引），90 天稳定在秒级。
    since = (today - dt.timedelta(days=90)).isoformat()
    shard_filled: dict[str, int] = {}
    shard_unreadable: list[str] = []

    # **没有可比源就根本不取数**：锚点把候选全排掉时（生产现状：akshare 异锚），
    # 再去读分片只会白付一次超时，还把结论污染成 CROSS_SOURCE_UNREADABLE。
    # 判据三此时该说的只有一句"无可比源"，而不是"读不到"。
    fill_targets = [owner, *comparable] if comparable else []
    for cand in fill_targets:
        try:
            extra = _shard_series(cand, codes, basket, since)
        except Exception:  # noqa: BLE001 - 读不到 ≠ 没有
            shard_unreadable.append(cand.source_id)
            continue
        if not extra:
            continue
        shard_filled[cand.source_id] = len(extra)
        for (symbol, date), value in extra.items():
            pivot.setdefault((symbol, date), {})[cand.source_id] = value


    out: list[Violation] = []
    compared = 0
    worst: dict[str, Any] = {}
    for (symbol, date), by_source in sorted(pivot.items()):
        if family_owner not in by_source:
            continue
        base = by_source[family_owner]
        if not base:
            continue
        for source, value in by_source.items():
            if source == family_owner:
                continue
            compared += 1
            diff_pct = abs(value - base) / abs(base) * 100.0
            if diff_pct > threshold_pct:
                if not worst or diff_pct > worst.get("diff_pct", 0):
                    worst = {
                        "symbol": symbol, "date": date, "diff_pct": round(diff_pct, 4),
                        "base_source": family_owner, "base": base,
                        "other_source": source, "other": value,
                    }
                out.append(
                    Violation(
                        code="CALIBER_DIVERGENCE",
                        judgment="cross_source",
                        detail=(
                            f"{symbol}@{date} 两源价差 {diff_pct:.4f}% 超阈值 {threshold_pct}%"
                            f"（{family_owner}={base} vs {source}={value}）"
                        ),
                        fix=(
                            "两源复权锚点不同：不得混读、不得择一继续。按源各自的锚点分别出数，"
                            "或在声明层把差异登记为口径事件"
                        ),
                        evidence={
                            "symbol": symbol, "date": date,
                            "base_source": family_owner, "base": base,
                            "other_source": source, "other": value,
                            "diff_pct": round(diff_pct, 4),
                        },
                    )
                )

    if anchor_mismatch:
        out.append(
            Violation(
                code="CROSS_SOURCE_ANCHOR_MISMATCH",
                judgment="cross_source",
                detail=(
                    f"以下源与首选源 {family_owner} 的复权锚点不同，价差不可直接比较（已从比对中排除）："
                    + ", ".join(
                        f"{c.source_id}(anchor={getattr(c, 'adjustment_anchor', None)!r})"
                        for c in anchor_mismatch
                    )
                ),
                fix=(
                    f"把两源锚点对齐（首选源锚点={owner_anchor!r}）后再比对；"
                    "或确认这里本就该比两种口径——那属于口径决策，不是数据修数"
                ),
                evidence={
                    "family_owner": family_owner, "family_owner_anchor": owner_anchor,
                    "excluded": {
                        c.source_id: getattr(c, "adjustment_anchor", None) for c in anchor_mismatch
                    },
                },
            )
        )

    checked = {
        "family_owner": family_owner,
        "family_owner_anchor": owner_anchor,
        "compared_sources": [c.source_id for c in comparable],
        "excluded_anchor_mismatch": {
            c.source_id: getattr(c, "adjustment_anchor", None) for c in anchor_mismatch
        },
        "compared_pairs": compared,
        "threshold_pct": threshold_pct,
        "worst": worst or None,
        # 哪些源是从自己的库补的行、补了多少。没这一项，"compared==0"就分不清
        # "两源真的没有同日数据"与"我压根没读到那一源"。
        "shard_filled": shard_filled,
        "shard_unreadable": shard_unreadable,
    }
    if not comparable:
        # 唯一候选被锚点排除 → 已在上面报 ANCHOR_MISMATCH，这里不再补第二条。
        pass
    elif compared == 0 and shard_unreadable:
        # 读不到 ≠ 没有重叠：这条必须报"判不了"，不能报"无重叠"。
        out.append(
            Violation(
                code="CROSS_SOURCE_UNREADABLE",
                judgment="cross_source",
                detail=(
                    "跨源判定无法成立：以下源在自己的库里读不到参照篮数据（超时/不可达）："
                    + ", ".join(shard_unreadable)
                ),
                fix="确认该源的分片库可达且 source_used 有行；这是读面问题，不是口径问题",
                evidence={"unreadable": shard_unreadable, "shard_filled": shard_filled},
            )
        )
    elif compared == 0:
        out.append(
            Violation(
                code="NO_CROSS_SOURCE_OVERLAP",
                judgment="cross_source",
                detail=(
                    f"首选源 {family_owner} 与 "
                    f"{', '.join(c.source_id for c in comparable)} 在参照篮内没有同日重叠，"
                    "跨源判定无法成立（空比对不是通过）"
                ),
                fix="确认两源的参照篮与概念码重合；若某一源确实无数据，这是一条真实的缺口",
                evidence=checked,
            )
        )
    return out, checked


# ---------------------------------------------------------------- main


def selfcheck() -> int:
    """自检：证明三条判据**都能失败**。

    为什么 CI 只跑这个而不跑真实判定：CI 里没有生产数据，判据会因"没有数据"而红，
    那是环境红不是口径红——把两者混成一个作业，得到的闸要么永远红（被忽略）要么
    永远绿（不设防）。这里造两个场景，验证"该绿时绿、该红时红"，闸本身才可信。

    退出码 0 = 两条都符合预期；1 = 自检不成立（探针坏了）。
    """
    import tempfile

    from fd_open_data_mcp import db as dbmod
    from fd_open_data_mcp.models import Base, Concept, Entity, SemanticObservation
    from fd_open_data_mcp.price_sources.declarations import SourceDeclaration, WatermarkProbe
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    def _fresh_session():
        d = tempfile.mkdtemp()
        eng = create_engine(f"sqlite:///{d}/sc.db")
        Base.metadata.create_all(eng)
        return sessionmaker(bind=eng)()  # type: ignore[call-arg]

    def _seed(session, *, with_index: bool, akshare_value: str):
        from datetime import date
        cs = Concept(code="price.close", entity_type="stock", frequency="daily", unit="CNY")
        ci = Concept(code="price.close", entity_type="index", frequency="daily", unit="point")
        e = Entity(entity_type="stock", code="600036", name_zh="招商银行")
        idx = Entity(entity_type="index", code="000300", name_zh="沪深300")
        session.add_all([cs, ci, e, idx]); session.flush()
        today = date.today().isoformat()
        session.add_all([
            SemanticObservation(concept_id=cs.id, entity_type="stock", entity_id=e.id,
                                date=today, granularity="day", value="36.73",
                                source_used="astock_daily"),
            SemanticObservation(concept_id=cs.id, entity_type="stock", entity_id=e.id,
                                date=today, granularity="day", value=akshare_value,
                                source_used="akshare"),
        ])
        if with_index:
            session.add(SemanticObservation(
                concept_id=ci.id, entity_type="index", entity_id=idx.id, date=today,
                granularity="day", value="3980.1", source_used="index-seed"))
        session.commit()

    reg = _reg_for_selfcheck(SourceDeclaration, WatermarkProbe)
    checks: list[tuple[str, bool]] = []

    # 场景 A：三路同源 + 指数有观测 + 两源同值 → 应全绿
    s = _fresh_session()
    reg_same = _reg_for_selfcheck(SourceDeclaration, WatermarkProbe, same_anchor=True)
    _seed(s, with_index=True, akshare_value="36.73")
    v1, _ = judge_three_paths(reg, s, {"trading": "astock_daily", "research": "astock_daily",
                                       "settle": "astock_daily"}, dt.date.today())
    v2, _ = judge_index(s, reg, dt.date.today())
    v3, _ = judge_divergence(s, reg_same, dt.date.today(), DEFAULT_THRESHOLD_PCT)
    s.close()
    checks.append(("全绿场景无违规", not (v1 or v2 or v3)))

    # 场景 A2：两源锚点不同 → 判据三必须报"不可比"，既不算分歧也不算通过。
    # **必须用不同的价**（37.73 vs 36.73）：同价时即便不过滤也产不出价差，
    # 那条断言就变成恒真——恒真的判据等于没有判据（本轮实测到过）。
    s2 = _fresh_session()
    _seed(s2, with_index=True, akshare_value="37.73")
    v3b, _ = judge_divergence(s2, reg, dt.date.today(), DEFAULT_THRESHOLD_PCT)
    s2.close()
    checks.append(("异锚报不可比（不冒充通过）",
                   any(v.code == "CROSS_SOURCE_ANCHOR_MISMATCH" for v in v3b)
                   and not any(v.code == "CALIBER_DIVERGENCE" for v in v3b)))

    # 场景 B：指数零观测 + 两源价差超阈 + 研究路换源 → 三条判据都必须失败
    s = _fresh_session()
    _seed(s, with_index=False, akshare_value="37.73")
    w1, _ = judge_three_paths(reg, s, {"trading": "astock_daily", "research": "akshare",
                                       "settle": "astock_daily"}, dt.date.today())
    w2, _ = judge_index(s, reg, dt.date.today())
    # 判据三"能红"必须用**同锚**注册表：异锚下正确结论是不可比（见场景 A2），
    # 拿异锚验"能红"会把两件事混成一件——这正是它在这里一直假绿/假红的原因。
    w3, _ = judge_divergence(s, reg_same, dt.date.today(), DEFAULT_THRESHOLD_PCT)
    s.close()
    checks.append(("判据一能红", any(v.judgment == "three_paths" for v in w1)))
    checks.append(("判据二能红", any(v.code == "INDEX_SERIES_ABSENT" for v in w2)))
    checks.append(("判据三能红", any(v.code == "CALIBER_DIVERGENCE" for v in w3)))

    # 判据四：开关断言。四种情形都要验——否则这条判据本身也可能是恒绿的。
    import os as _os

    _prev = _os.environ.get("PRICE_CALIBER_ENFORCEMENT")
    try:
        _os.environ["PRICE_CALIBER_ENFORCEMENT"] = "warn"
        e_warn, _ = judge_enforcement_mode()
        checks.append(("判据四：warn 时报违规",
                       any(v.code == "ENFORCEMENT_STILL_WARN" for v in e_warn)))

        del _os.environ["PRICE_CALIBER_ENFORCEMENT"]
        e_unset, _ = judge_enforcement_mode()
        checks.append(("判据四：未设置按 warn 处理并报违规",
                       any(v.code == "ENFORCEMENT_STILL_WARN" for v in e_unset)))

        _os.environ["PRICE_CALIBER_ENFORCEMENT"] = "refuse"
        e_refuse, _ = judge_enforcement_mode()
        checks.append(("判据四：refuse 时放行", e_refuse == []))

        _os.environ["PRICE_CALIBER_ENFORCEMENT"] = "maybe"
        e_bad, _ = judge_enforcement_mode()
        checks.append(("判据四：非法取值报违规",
                       any(v.code == "ENFORCEMENT_MODE_INVALID" for v in e_bad)))
    finally:
        if _prev is None:
            _os.environ.pop("PRICE_CALIBER_ENFORCEMENT", None)
        else:
            _os.environ["PRICE_CALIBER_ENFORCEMENT"] = _prev

    bad = [name for name, ok in checks if not ok]
    for name, ok in checks:
        print(f"  {'✓' if ok else '✗'} {name}")
    if bad:
        print(f"✗ selfcheck 不成立：{bad}", file=sys.stderr)
        return 1
    print("✓ selfcheck 通过：四条判据该绿时绿、该红时红（含开关断言的四种取值）")
    return 0


def _reg_for_selfcheck(SourceDeclaration, WatermarkProbe, *, same_anchor: bool = False):
    """自检用的最小注册表：两条存量源 + 指数种子，形状与生产声明一致。

    ``same_anchor=True`` 让 akshare 与首选源同锚——自检的"全绿"场景必须用它：
    生产里两源锚点不同（2026-07-10 vs per_fetch），那种情况下判据三**本就该**
    报 `CROSS_SOURCE_ANCHOR_MISMATCH`（不可比要明说），不该被当成全绿。
    """
    from fd_open_data_mcp.price_sources.declarations import SourceRegistry

    def decl(sid, *, etype, adjustment, anchor, preferred=()):
        return SourceDeclaration(
            source_id=sid, instrument_types=("stock",) if etype == "stock" else ("index",),
            markets=("XSHG",), adjustment=adjustment, frequency="daily",
            license_state="legacy",
            watermark_probe=WatermarkProbe(entity_type=etype, concept_codes=("price.close",),
                                           reference_basket=("600036",) if etype == "stock"
                                           else ("000300",), lookback_days=400),
            access=__import__("fd_open_data_mcp.price_sources.declarations",
                              fromlist=["AccessSpec"]).AccessSpec(
                kind="federated_view", dsn_env="ASTOCK_DSN"),
            adjustment_anchor=anchor, db_source=sid, preferred_for=preferred,
        )

    srcs = {
        "astock_daily": decl("astock_daily", etype="stock", adjustment="qfq",
                             anchor="2026-07-10", preferred=("price.stock.daily",)),
        "akshare": decl("akshare", etype="stock", adjustment="qfq",
                        anchor="2026-07-10" if same_anchor else "per_fetch"),
        "index-seed": decl("index-seed", etype="index", adjustment="not_applicable",
                           anchor=None, preferred=("price.index.daily",)),
    }
    owner = {f: sid for sid, d in srcs.items() for f in d.preferred_for}
    return SourceRegistry(schema_version="1", sources=srcs,
                          price_families=("price.stock.daily", "price.index.daily"),
                          _family_owner=owner)


def _emit_violations_before_fatal(report: "Report") -> None:
    """fatal 退出前，把已经得出的违规先吐出来（写 stderr）。

    为什么必须这么做：探针有几条判据**不依赖库**（判据四是纯环境断言）。库或查询失败时
    直接 `return 2`，会把这些已经确凿的违规一起吞掉——于是"库挂了"掩盖了"开关还停在 warn"。
    退出码 2 表示"这次跑不完整"，不等于"没有任何违规成立"。
    """
    if report.ok:
        return
    print("\n（以下违规与库无关，仍然成立）", file=sys.stderr)
    for v in report.violations:
        print(v.line(), file=sys.stderr)


def judge_enforcement_mode() -> tuple[list[Violation], dict]:
    """判据四：口径强制模式必须已收敛到 `refuse`（add-price-supply-and-caliber 6.3）。

    ## 为什么这是一条判据而不是一条建议

    `warn` 是迁移期的观察窗：判据不一致时只告警、不拦。它的存在是有意的，但它**不能是终点**
    ——一个长期停在 `warn` 的开关，等于把"口径不一致"降级成日志噪音，而真正需要它的时刻
    恰恰是没人看日志的时候。

    本判据把"还没切"变成可见的违规：只要环境里不是 `refuse`，探针就红。这样收敛工作
    不会因为"反正没报错"而被无限期推后。

    ## 只读

    只读环境变量，不改配置、不写文件。切开关是运维动作（6.3 的人工部分），本判据负责
    让"没切"这件事无法被忽略。
    """
    import os

    raw = os.environ.get("PRICE_CALIBER_ENFORCEMENT")
    mode = (raw or "warn").strip().lower()
    checked = {"mode": mode, "raw": raw}

    if mode == "refuse":
        return [], checked

    if mode not in ("warn", "refuse"):
        return [
            Violation(
                code="ENFORCEMENT_MODE_INVALID",
                judgment="enforcement",
                detail=f"PRICE_CALIBER_ENFORCEMENT={raw!r} 不是合法取值（只认 warn / refuse）",
                fix="改为 refuse；写错词会让开关静默失效，比不设开关更危险",
                evidence=checked,
            )
        ], checked

    return [
        Violation(
            code="ENFORCEMENT_STILL_WARN",
            judgment="enforcement",
            detail=(
                "PRICE_CALIBER_ENFORCEMENT 仍是 warn"
                f"{'（未设置，按默认 warn 处理）' if raw is None else ''}："
                "口径不一致只告警不拦，等于把不一致降级成日志噪音"
            ),
            fix=(
                "先完成 6.1（三路收敛到同一 source_id 与同一水位），再把该变量切到 refuse；"
                "收敛前后端口径不一致是已知状态，切早了会拦住正常结算"
            ),
            evidence=checked,
        )
    ], checked


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Price calibre consistency gate (read-only)")
    ap.add_argument("--trading", default=None, help="交易路 source_id")
    ap.add_argument("--research", default=None, help="研究取价 source_id")
    ap.add_argument("--settle", default=None, help="结算取价 source_id")
    ap.add_argument("--threshold-pct", type=float, default=DEFAULT_THRESHOLD_PCT)
    ap.add_argument("--declarations", default=None, help="声明文件路径")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--today", default=None, help="窗口基准日（YYYY-MM-DD），仅测试用")
    ap.add_argument(
        "--selfcheck", action="store_true",
        help="自检：用临时 SQLite 造「全绿」与「必红」两个场景，验证三条判据都能失败。"
             "CI 跑这个；真实环境一致性仍由人工/调度跑不带 --selfcheck 的形态。",
    )
    args = ap.parse_args(argv)

    if args.selfcheck:
        return selfcheck()

    try:
        from fd_open_data_mcp.price_sources import load_registry

        reg = load_registry(args.declarations)
    except Exception as exc:
        print(f"[fatal] 声明不可加载: {exc}", file=sys.stderr)
        return 2

    report = Report()
    # 判据四**不碰库**，所以在连库之前先跑：库不可达不该顺手掩盖"开关还停在 warn"。
    # 一个不依赖外部资源的判据，因为别的判据跑不了而沉默，是最没道理的一种沉默。
    v4, c4 = judge_enforcement_mode()
    for v in v4:
        report.add(v)

    try:
        from fd_open_data_mcp.db import get_database

        today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()
        session = get_database().get_session()
    except Exception as exc:
        print(f"[fatal] 库不可达: {exc}", file=sys.stderr)
        _emit_violations_before_fatal(report)
        return 2

    paths = {
        "trading": args.trading or _env_source(PATH_ENV["trading"]),
        "research": args.research or _env_source(PATH_ENV["research"]),
        "settle": args.settle or _env_source(PATH_ENV["settle"]),
    }

    try:
        v1, c1 = judge_three_paths(reg, session, paths, today)
        v2, c2 = judge_index(session, reg, today)
        v3, c3 = judge_divergence(session, reg, today, args.threshold_pct)
    except Exception as exc:  # 探针自身故障必须与"有违规"区分
        print(f"[fatal] 探针执行失败: {exc}", file=sys.stderr)
        _emit_violations_before_fatal(report)
        return 2
    finally:
        session.close()

    for v in v1 + v2 + v3:
        report.add(v)
    report.checked = {"three_paths": c1, "index_series": c2,
                      "cross_source": c3, "enforcement": c4}

    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        if report.ok:
            print("OK: 三条判据全部通过（三路同源同水位 / 指数族有观测 / 跨源价差未超阈）")
        else:
            print(f"FAIL: {len(report.violations)} 条违规\n")
            for v in report.violations:
                print(v.line())
                print()

    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
