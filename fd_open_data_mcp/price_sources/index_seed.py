"""指数日线的一次性种子导入（openspec change: add-price-supply-and-caliber, task 5.2）。

## 为什么需要这条通道

基准相对指标（超额 / 跟踪误差 / IR / 组合 alpha）全部依赖指数日线，而实测指数实体与
index 概念族**全库零观测**；价格抓取自 2026-10-07 政策冻结，不能靠重开抓取补。所以必须先
有一条**不依赖采购、不依赖抓取**的导入路：把一份已有的指数快照导入联邦视图。

## 三条纪律

1. **身份按实体类型分开**：``000300`` 既是沪深300指数的代码，也是一只股票的代码
   （厦门港务，实测同码碰撞）。解析一律按 ``(entity_type='index', code)``，永不按裸码。
2. **许可状态如实**：导入行带 ``source_used='index-seed'``、声明为 ``provisional``
   （可用但待持牌源替换）；对外交付必须显式声明来源与"待替换"，由 ``delivery`` 闸执行。
3. **不编数**：逐行校验，坏行进 ``rejected`` 并给原因；没有值的格子留空，不插值、不前值填充。
   重复导入幂等（``ON CONFLICT DO NOTHING``），因为一次性通道也会被人重跑。

复权：指数不做复权，声明层已显式登记 ``adjustment=not_applicable``——空值与"不适用"是
两件事，本仓最贵的一类混淆就在这里。
"""
from __future__ import annotations

import csv
import datetime as dt
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from fd_open_data_mcp.models import Concept, Entity, SemanticObservation
from fd_open_data_mcp.semantic.dates import obs_month_of

logger = logging.getLogger(__name__)

#: field -> (concept code, unit)。指数点位没有货币单位，用 ``point``。
INDEX_CONCEPT_MAP = {
    "open": ("price.open", "point"),
    "close": ("price.close", "point"),
    "high": ("price.high", "point"),
    "low": ("price.low", "point"),
    "volume": ("price.volume", "shares"),
    "amount": ("price.amount", "currency"),
}

DEFAULT_SOURCE_ID = "index-seed"

#: 指数代码形如 000300 / 399001，可能带 .SS/.SH/.SZ/.SHE 后缀（结算的基准映射用带后缀形态）。
_SYMBOL_RE = re.compile(r"^(?P<code>\d{6})(?:\.(?P<venue>SH|SS|SZ|SHE))?$", re.I)


class SeedRowError(ValueError):
    """单行不合法。整批导入不因此失败，该行进 ``rejected``。"""


@dataclass
class SeedReport:
    source_id: str
    rows_in: int = 0
    observations_written: int = 0
    observations_existing: int = 0
    entities_created: tuple[str, ...] = ()
    concepts_created: tuple[str, ...] = ()
    rejected: list[dict[str, Any]] = field(default_factory=list)
    dry_run: bool = False

    def to_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "dry_run": self.dry_run,
            "rows_in": self.rows_in,
            "observations_written": self.observations_written,
            "observations_existing": self.observations_existing,
            "entities_created": list(self.entities_created),
            "concepts_created": list(self.concepts_created),
            "rejected_count": len(self.rejected),
            "rejected": self.rejected,
        }

    @property
    def ok(self) -> bool:
        return not self.rejected


def normalize_index_symbol(raw: str) -> str:
    """``000300`` / ``000300.SS`` / ``399001.SZ`` -> 裸六位码。

    后缀只用于消歧，落库身份是 ``(entity_type='index', code)``；后缀在这里丢弃，
    因为库里的 index 实体就是裸码（实测 ``9676 index 沪深300指数``）。
    """
    m = _SYMBOL_RE.match(str(raw or "").strip())
    if not m:
        raise SeedRowError(f"symbol {raw!r} is not a 6-digit index code (optional .SS/.SH/.SZ)")
    return m.group("code")


def ensure_index_concepts(s, *, dry_run: bool = False) -> tuple[dict[str, int], list[str]]:
    """幂等注册 index 概念族。返回 ``({field: concept_id}, created_codes)``。

    同码多行（历史重复）时取**最小的非废弃 id**——与 ``_stock_concept_ids`` 同一口径，
    避免同一概念在不同批次落到不同 id 上。
    """
    codes = {code for code, _ in INDEX_CONCEPT_MAP.values()}
    existing = (
        s.execute(
            select(Concept.id, Concept.code).where(
                Concept.code.in_(list(codes)),
                Concept.entity_type == "index",
                Concept.deprecated.is_(False),
            )
        ).all()
    )
    by_code: dict[str, int] = {}
    for cid, code in sorted(existing, key=lambda r: r[0]):
        by_code.setdefault(code, cid)

    created: list[str] = []
    units = {code: unit for code, unit in INDEX_CONCEPT_MAP.values()}
    # dry_run 也要把"将要创建的概念"登记进映射，否则调用方会以为概念族不完整。
    # 用负占位 id：它明显不是真实主键，不会被误当成已落库的概念。
    placeholders = {code: -(i + 1) for i, code in enumerate(sorted(codes))}
    for code in sorted(codes):
        if code in by_code:
            continue
        if dry_run:
            by_code[code] = placeholders[code]
            created.append(code)
            continue
        c = Concept(
            code=code,
            entity_type="index",
            frequency="daily",
            unit=units[code],
            name_en=f"{code} (index)",
            name_zh=code,
        )
        s.add(c)
        s.flush()
        by_code[code] = c.id
        created.append(code)

    field_of = {f: by_code.get(code) for f, (code, _u) in INDEX_CONCEPT_MAP.items()}
    return {k: v for k, v in field_of.items() if v is not None}, created


def ensure_index_entity(s, code: str, *, name_zh: str | None = None, dry_run: bool = False) -> tuple[int, bool]:
    """按 ``(entity_type='index', code)`` 取或建实体。返回 ``(entity_id, created)``。

    **必须**带 ``entity_type='index'``：``000300`` 同时是一只股票（厦门港务）的代码，
    裸码查会撞到另一类实体。
    """
    row = s.execute(
        select(Entity.id).where(Entity.entity_type == "index", Entity.code == code)
    ).scalar()
    if row:
        return int(row), False
    if dry_run:
        return -1, True
    e = Entity(entity_type="index", code=code, name_zh=name_zh or code)
    s.add(e)
    s.flush()
    return int(e.id), True


def _parse_date(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        raise SeedRowError("date is empty")
    try:
        return dt.date.fromisoformat(text[:10]).isoformat()
    except ValueError as exc:
        raise SeedRowError(f"date {raw!r} is not ISO (YYYY-MM-DD)") from exc


def _parse_value(field_name: str, raw: Any) -> str | None:
    """数值校验但不编数：空串 -> None（该点不落行），非数字即拒绝。"""
    if raw is None:
        return None
    text = str(raw).strip()
    if text == "" or text.lower() in ("na", "nan", "null", "-"):
        return None
    try:
        float(text)
    except ValueError as exc:
        raise SeedRowError(f"{field_name}={raw!r} is not numeric") from exc
    return text


def import_index_seed(
    s: Session,
    rows: Iterable[Mapping[str, Any]],
    *,
    source_id: str = DEFAULT_SOURCE_ID,
    dry_run: bool = False,
) -> SeedReport:
    """把指数日线快照写入联邦视图。

    行字段：``date``（必填）、``symbol``（必填）、以及 ``open/close/high/low/volume/amount``
    的子集。没有值的字段不落行（不是写空值）——"没有"与"是零"必须可区分。
    """
    report = SeedReport(source_id=source_id, dry_run=dry_run)
    fields, created_concepts = ensure_index_concepts(s, dry_run=dry_run)
    report.concepts_created = tuple(created_concepts)
    missing = [f for f in ("close",) if f not in fields]
    if missing:
        # close 是唯一硬要求：一个点只有它的"价格"含义是确定的。
        raise SeedRowError(
            f"index concept family is incomplete (missing {missing}); refusing to import"
        )

    created_entities: set[str] = set()
    new_obs: list[SemanticObservation] = []

    for idx, row in enumerate(rows, start=1):
        report.rows_in += 1
        try:
            code = normalize_index_symbol(row.get("symbol", ""))
            date = _parse_date(row.get("date"))
        except SeedRowError as exc:
            report.rejected.append({"row": idx, "reason": str(exc), "raw": dict(row)})
            continue

        eid, created = ensure_index_entity(s, code, name_zh=row.get("name"), dry_run=dry_run)
        if created:
            created_entities.add(code)

        values: dict[str, str] = {}
        bad = False
        for field_name in fields:
            try:
                v = _parse_value(field_name, row.get(field_name))
            except SeedRowError as exc:
                report.rejected.append({"row": idx, "reason": str(exc), "raw": dict(row)})
                bad = True
                break
            if v is not None:
                values[field_name] = v
        if bad:
            continue
        if not values:
            report.rejected.append(
                {"row": idx, "reason": "no usable OHLCV field in row", "raw": dict(row)}
            )
            continue

        for field_name, value in values.items():
            cid = fields[field_name]
            unit = dict(INDEX_CONCEPT_MAP.values())[INDEX_CONCEPT_MAP[field_name][0]]
            new_obs.append(
                SemanticObservation(
                    concept_id=cid,
                    entity_type="index",
                    entity_id=eid,
                    date=date,
                    granularity="day",
                    value=value,
                    unit=unit,
                    source_used=source_id,
                    obs_month=obs_month_of(date),
                )
            )

    report.entities_created = tuple(sorted(created_entities))

    if dry_run:
        report.observations_written = len(new_obs)
        return report

    if new_obs:
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        dialect = s.bind.dialect.name if s.bind is not None else "sqlite"
        keys = ["concept_id", "entity_type", "entity_id", "date", "granularity", "source_used"]
        payload = [
            {
                "concept_id": o.concept_id,
                "entity_type": o.entity_type,
                "entity_id": o.entity_id,
                "date": o.date,
                "granularity": o.granularity,
                "value": o.value,
                "unit": o.unit,
                "source_used": o.source_used,
                "obs_month": o.obs_month,
                "fetched_at": dt.datetime.now(dt.timezone.utc).replace(tzinfo=None),
            }
            for o in new_obs
        ]
        insert = pg_insert if dialect.startswith("postgres") else sqlite_insert
        stmt = insert(SemanticObservation).values(payload)
        stmt = stmt.on_conflict_do_nothing(index_elements=keys).returning(SemanticObservation.id)
        written = len(s.execute(stmt).all())
        s.commit()
        report.observations_written = written
        report.observations_existing = len(payload) - written

    return report


def read_seed_csv(path: str | Path) -> list[dict[str, str]]:
    """读 CSV 快照。表头即字段名；空行跳过。"""
    with open(path, newline="", encoding="utf-8") as fh:
        return [row for row in csv.DictReader(fh) if any((v or "").strip() for v in row.values())]


def seed_watermarks(s: Session, source_id: str = DEFAULT_SOURCE_ID, codes: Sequence[str] | None = None) -> dict[str, dict]:
    """导入后自检：每个指数代码的观测数与最新日期。用于任务 5.2 的验收。"""
    from sqlalchemy import func

    stmt = (
        select(
            Entity.code,
            func.count(SemanticObservation.id),
            func.max(SemanticObservation.date),
        )
        .select_from(SemanticObservation)
        .join(Entity, Entity.id == SemanticObservation.entity_id)
        .where(
            SemanticObservation.entity_type == "index",
            SemanticObservation.source_used == source_id,
            Entity.entity_type == "index",
        )
        .group_by(Entity.code)
    )
    if codes:
        stmt = stmt.where(Entity.code.in_([normalize_index_symbol(c) for c in codes]))
    return {
        code: {"observations": int(count), "latest": latest}
        for code, count, latest in s.execute(stmt).all()
    }
