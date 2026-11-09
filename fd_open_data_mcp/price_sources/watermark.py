"""水位派生：每个行情源「已证可用」的最新日期，由数据本身算出来。

为什么不在声明里写水位（design D1）：写死的水位会变成假绿。本项目反复踩过
"配置值在启动/入队期被快照"（``config-value-snapshotted-at-start``），一个写死的
``as_of_max`` 会让消费方以为源还活着。

为什么用参照篮而不是全窗 ``group by``（design Context 第 3 条）：实测全市场全窗扫描
>200s 超时，``date = any(30 天)`` 35.9s，参照篮（5 只 × 全年平日）4.9s/年。

参照篮给出的水位是**下界**：某只篮内标的没数据不代表别的标的也没有。这个方向是安全
的——低估只会让陈旧更早暴露，不会把陈旧冒充成新鲜。需要精确值时用 ``full=True``
（代价是慢，且明确标注 ``method="full_scan"``）。

只读：本模块只发 SELECT，不写任何行、不建任何表。
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fd_open_data_mcp.models import Concept, Entity, SemanticObservation

logger = logging.getLogger(__name__)

#: 水位取不到时的状态值。刻意不用 None/空串——"读不到"必须能与"确实是空"区分
#: （本仓最贵的一类 bug：把读不到当成不存在）。
NO_DATA = "no_data"
UNREACHABLE = "unreachable"

METHOD_BASKET = "reference_basket"
METHOD_FULL = "full_scan"


@dataclass(frozen=True)
class Watermark:
    """一个源的实时水位。

    ``as_of`` 为 ``None`` 时 ``status`` 说明为什么没有：
    ``no_data``（篮内无行）或 ``unreachable``（查询失败）。二者不可混为一谈。
    """

    source_id: str
    as_of: str | None
    status: str  # ok | no_data | unreachable
    method: str
    basket: tuple[str, ...] = ()
    lookback_days: int = 0
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "as_of": self.as_of,
            "status": self.status,
            "method": self.method,
            "basket": list(self.basket),
            "lookback_days": self.lookback_days,
            "detail": self.detail,
        }


def _basket_entity_ids(
    s: Session, entity_type: str, codes: Sequence[str]
) -> dict[int, str]:
    """篮内代码 → entity_id。按 entity_type 区分，避免同码碰撞（000300 既是指数又是股票）。"""
    if not codes:
        return {}
    rows = s.execute(
        select(Entity.id, Entity.code).where(
            Entity.entity_type == entity_type, Entity.code.in_(list(codes))
        )
    ).all()
    return {int(eid): str(code) for eid, code in rows}


def derive_watermark(
    s: Session,
    *,
    source_id: str,
    entity_type: str,
    concept_codes: Sequence[str],
    reference_basket: Sequence[str],
    lookback_days: int = 400,
    full: bool = False,
    today: dt.date | None = None,
) -> Watermark:
    """为一个源取水位。``full=True`` 时忽略参照篮、扫该源全量（慢）。"""
    cutoff = ((today or dt.date.today()) - dt.timedelta(days=lookback_days)).isoformat()

    conditions = [
        SemanticObservation.source_used == source_id,
        SemanticObservation.entity_type == entity_type,
        Concept.code.in_(list(concept_codes)),
        SemanticObservation.date >= cutoff,
    ]

    if not full:
        ids = _basket_entity_ids(s, entity_type, reference_basket)
        if not ids:
            # 篮内一个标的都没解析到：这是"库与声明对不上"，不是"源没数据"。
            return Watermark(
                source_id=source_id,
                as_of=None,
                status=NO_DATA,
                method=METHOD_BASKET,
                basket=tuple(reference_basket),
                lookback_days=lookback_days,
                detail=(
                    "reference basket produced no entity ids for "
                    f"entity_type={entity_type!r}; the declarations and the store "
                    "disagree about which symbols exist"
                ),
            )
        conditions.append(SemanticObservation.entity_id.in_(list(ids)))

    try:
        row = s.execute(
            select(func.max(SemanticObservation.date))
            .select_from(SemanticObservation)
            .join(Concept, Concept.id == SemanticObservation.concept_id)
            .where(*conditions)
        ).scalar()
    except Exception as exc:  # 查询失败 ≠ 没有数据
        logger.warning("watermark query failed for %s: %s", source_id, exc)
        return Watermark(
            source_id=source_id,
            as_of=None,
            status=UNREACHABLE,
            method=METHOD_FULL if full else METHOD_BASKET,
            basket=tuple(reference_basket),
            lookback_days=lookback_days,
            detail=str(exc),
        )

    if not row:
        return Watermark(
            source_id=source_id,
            as_of=None,
            status=NO_DATA,
            method=METHOD_FULL if full else METHOD_BASKET,
            basket=tuple(reference_basket),
            lookback_days=lookback_days,
            detail=f"no observations since {cutoff}",
        )

    return Watermark(
        source_id=source_id,
        as_of=str(row),
        status="ok",
        method=METHOD_FULL if full else METHOD_BASKET,
        basket=tuple(reference_basket),
        lookback_days=lookback_days,
    )


def derive_watermarks(
    s: Session,
    declarations: Mapping[str, object],
    *,
    full: bool = False,
    today: dt.date | None = None,
) -> dict[str, Watermark]:
    """批量取水位。``declarations`` 为 ``{source_id: SourceDeclaration}``。

    查询用 ``decl.db_source``（库内 ``source_used`` 的实际取值），结果的 ``source_id``
    仍用注册表 key——口径变体可以共用同一个库源而不混淆身份。
    """
    out: dict[str, Watermark] = {}
    for sid, decl in declarations.items():
        probe = decl.watermark_probe  # type: ignore[attr-defined]
        db_source = getattr(decl, "db_source", None) or sid
        mark = derive_watermark(
            s,
            source_id=db_source,
            entity_type=probe.entity_type,
            concept_codes=probe.concept_codes,
            reference_basket=probe.reference_basket,
            lookback_days=probe.lookback_days,
            full=full,
            today=today,
        )
        out[sid] = Watermark(
            source_id=sid,
            as_of=mark.as_of,
            status=mark.status,
            method=mark.method,
            basket=mark.basket,
            lookback_days=mark.lookback_days,
            detail=mark.detail,
        )
    return out
