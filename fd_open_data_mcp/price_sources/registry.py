"""行情源注册表的对外出口：声明 + 实时水位合并成一次查询。

spec ``price-source-registry`` 要求「消费方只认这一个出口，禁止各自缓存」（D1）。
本模块不缓存任何水位——每次调用都从库派生，因为缓存水位正是"三路读到三个水位"的
来源。

消费方用法：

    from fd_open_data_mcp.price_sources import list_price_sources, resolve_for_family
    listing = list_price_sources()              # 声明 + 水位
    src = resolve_for_family("price.stock.daily")   # D7：必须显式声明过首选源
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Sequence

from fd_open_data_mcp.db import get_database
from fd_open_data_mcp.price_sources.declarations import (
    DeclarationError,
    FamilyNotClaimed,
    NotDeliverable,
    SourceDeclaration,
    SourceNotRegistered,
    SourceRegistry,
    load_registry,
)
from fd_open_data_mcp.price_sources.watermark import (
    NO_DATA,
    UNREACHABLE,
    Watermark,
    derive_watermark,
    derive_watermarks,
)

__all__ = [
    "DeclarationError",
    "FamilyNotClaimed",
    "NotDeliverable",
    "SourceDeclaration",
    "SourceNotRegistered",
    "SourceRegistry",
    "Watermark",
    "load_registry",
    "list_price_sources",
    "resolve_for_family",
    "resolve_source",
    "assert_deliverable",
    "NO_DATA",
    "UNREACHABLE",
]

_CACHED_REGISTRY: SourceRegistry | None = None
_CACHED_PATH: Path | None = None


def _registry(path: str | Path | None = None) -> SourceRegistry:
    """进程内只缓存**声明**（随镜像走的静态内容），永不缓存水位。"""
    global _CACHED_REGISTRY, _CACHED_PATH
    p = Path(path) if path else None
    if p is None and _CACHED_REGISTRY is not None:
        return _CACHED_REGISTRY
    reg = load_registry(p)
    if p is None:
        _CACHED_REGISTRY, _CACHED_PATH = reg, None
    return reg


def resolve_source(source_id: str, *, path: str | Path | None = None) -> SourceDeclaration:
    """按 id 取源声明；未注册即抛 :class:`SourceNotRegistered`，不回落。"""
    return _registry(path).get(source_id)


def resolve_for_family(family: str, *, path: str | Path | None = None) -> SourceDeclaration:
    """取某价格概念族的首选源；无唯一首选源即抛 :class:`FamilyNotClaimed`。"""
    return _registry(path).resolve_for_family(family)


def assert_deliverable(source_id: str, *, path: str | Path | None = None) -> SourceDeclaration:
    """对外交付许可闸；不合规即抛 :class:`NotDeliverable`。"""
    return _registry(path).assert_deliverable(source_id)


def list_price_sources(
    *,
    path: str | Path | None = None,
    source_ids: Sequence[str] | None = None,
    full_scan: bool = False,
    today: dt.date | None = None,
) -> dict:
    """声明 + 实时水位的合并视图。消费方唯一出口。

    Args:
        path: 声明文件路径（默认包内 ``declarations.yaml``）。
        source_ids: 只取这些源；给未注册 id 会抛错（便于尽早暴露拼写）。
        full_scan: 水位用全量扫描（精确但慢）；默认参照篮（下界）。
        today: 水位 lookback 的基准日，仅测试用。

    Returns:
        ``{schema_version, price_families, sources: [{...声明..., watermark: {...}}]}``
    """
    reg = _registry(path)
    declared: dict[str, SourceDeclaration] = dict(reg.sources)
    if source_ids is not None:
        wanted = [reg.get(sid) for sid in source_ids]  # 未注册即抛错
        declared = {d.source_id: d for d in wanted}

    s = get_database().get_session()
    try:
        marks = derive_watermarks(s, declared, full=full_scan, today=today)
    finally:
        s.close()

    return {
        "schema_version": reg.schema_version,
        "price_families": list(reg.price_families),
        "sources": [
            {**d.to_dict(), "watermark": marks[d.source_id].to_dict()}
            for d in declared.values()
        ],
    }


def watermark_for(source_id: str, *, path: str | Path | None = None) -> Watermark:
    """单个源的水位（研究/交易/结算三路必须读同一个对象，D-Context 第 2 条）。"""
    reg = _registry(path)
    decl = reg.get(source_id)
    probe = decl.watermark_probe
    s = get_database().get_session()
    try:
        mark = derive_watermark(
            s,
            source_id=decl.db_source or decl.source_id,
            entity_type=probe.entity_type,
            concept_codes=probe.concept_codes,
            reference_basket=probe.reference_basket,
            lookback_days=probe.lookback_days,
        )
    finally:
        s.close()
    return Watermark(
        source_id=decl.source_id,
        as_of=mark.as_of,
        status=mark.status,
        method=mark.method,
        basket=mark.basket,
        lookback_days=mark.lookback_days,
        detail=mark.detail,
    )
