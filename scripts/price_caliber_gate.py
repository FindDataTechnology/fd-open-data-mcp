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
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
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

    candidates = [
        d for d in reg.sources.values()
        if "stock" in d.instrument_types and d.source_id != family_owner
    ]
    if not candidates:
        return [], {"note": "only one stock source registered; nothing to cross-check"}

    owner = reg.get(family_owner)
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
        )
    ).all()

    # (symbol, date) -> {source: value}
    pivot: dict[tuple[str, str], dict[str, float]] = {}
    for _eid, symbol, date, source, value in rows:
        try:
            fv = float(value)
        except (TypeError, ValueError):
            continue
        pivot.setdefault((symbol, date), {})[source] = fv

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

    checked = {
        "family_owner": family_owner,
        "compared_pairs": compared,
        "threshold_pct": threshold_pct,
        "worst": worst or None,
    }
    if compared == 0:
        out.append(
            Violation(
                code="NO_CROSS_SOURCE_OVERLAP",
                judgment="cross_source",
                detail=(
                    f"首选源 {family_owner} 与 "
                    f"{', '.join(c.source_id for c in candidates)} 在参照篮内没有同日重叠，"
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
    _seed(s, with_index=True, akshare_value="36.73")
    v1, _ = judge_three_paths(reg, s, {"trading": "astock_daily", "research": "astock_daily",
                                       "settle": "astock_daily"}, dt.date.today())
    v2, _ = judge_index(s, reg, dt.date.today())
    v3, _ = judge_divergence(s, reg, dt.date.today(), DEFAULT_THRESHOLD_PCT)
    s.close()
    checks.append(("全绿场景无违规", not (v1 or v2 or v3)))

    # 场景 B：指数零观测 + 两源价差超阈 + 研究路换源 → 三条判据都必须失败
    s = _fresh_session()
    _seed(s, with_index=False, akshare_value="37.73")
    w1, _ = judge_three_paths(reg, s, {"trading": "astock_daily", "research": "akshare",
                                       "settle": "astock_daily"}, dt.date.today())
    w2, _ = judge_index(s, reg, dt.date.today())
    w3, _ = judge_divergence(s, reg, dt.date.today(), DEFAULT_THRESHOLD_PCT)
    s.close()
    checks.append(("判据一能红", any(v.judgment == "three_paths" for v in w1)))
    checks.append(("判据二能红", any(v.code == "INDEX_SERIES_ABSENT" for v in w2)))
    checks.append(("判据三能红", any(v.code == "CALIBER_DIVERGENCE" for v in w3)))

    bad = [name for name, ok in checks if not ok]
    for name, ok in checks:
        print(f"  {'✓' if ok else '✗'} {name}")
    if bad:
        print(f"✗ selfcheck 不成立：{bad}", file=sys.stderr)
        return 1
    print("✓ selfcheck 通过：三条判据该绿时绿、该红时红")
    return 0


def _reg_for_selfcheck(SourceDeclaration, WatermarkProbe):
    """自检用的最小注册表：两条存量源 + 指数种子，形状与生产声明一致。"""
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
        "akshare": decl("akshare", etype="stock", adjustment="qfq", anchor="per_fetch"),
        "index-seed": decl("index-seed", etype="index", adjustment="not_applicable",
                           anchor=None, preferred=("price.index.daily",)),
    }
    owner = {f: sid for sid, d in srcs.items() for f in d.preferred_for}
    return SourceRegistry(schema_version="1", sources=srcs,
                          price_families=("price.stock.daily", "price.index.daily"),
                          _family_owner=owner)


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

    try:
        from fd_open_data_mcp.db import get_database

        today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()
        session = get_database().get_session()
    except Exception as exc:
        print(f"[fatal] 库不可达: {exc}", file=sys.stderr)
        return 2

    report = Report()
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
        return 2
    finally:
        session.close()

    for v in v1 + v2 + v3:
        report.add(v)
    report.checked = {"three_paths": c1, "index_series": c2, "cross_source": c3}

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
