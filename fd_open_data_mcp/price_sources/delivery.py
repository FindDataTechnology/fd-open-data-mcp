"""许可与对外交付闸（task 5.3 / 5.4）。

两件事：

1. **持牌适配器形状**：只立接口与"未配置即拒绝"，不实现任何具体供应商。真实通道不提供
   不是"未实现"，是**明确的拒绝**——这个区别在合同与合规上都成立。
2. **provisional/seed 源的交付闸**：对外交付引用这类源时，**必须**带来源与"待替换"声明；
   缺声明即拒绝。许可状态以拒绝生效，不是警告——警告会被忽略，而"默认可用"正是要消灭的。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from fd_open_data_mcp.price_sources.declarations import (
    NOT_REPLACED_STATES,
    SourceDeclaration,
    SourceRegistry,
)


class LicensedAdapterNotConfigured(RuntimeError):
    """请求了一个没有配置的持牌源。绝不回落到 seed/自抓源。"""


class DeliveryAttributionMissing(PermissionError):
    """对外交付缺少必需的来源/待替换声明。"""


@runtime_checkable
class LicensedPriceAdapter(Protocol):
    """持牌行情适配器的形状（不实现具体供应商）。

    实现方只需要能按标的与窗口给出日线；口径（复权、锚点、因子来源）在声明层给出，
    不在适配器里另立一套。
    """

    source_id: str

    def daily_bars(self, symbol: str, start: str, end: str) -> list[dict]:  # pragma: no cover
        ...


#: 已注册的持牌适配器。刻意留空：采购到位前，任何持牌源都应走"未配置即拒绝"。
_REGISTERED: dict[str, LicensedPriceAdapter] = {}


def register_licensed_adapter(adapter: LicensedPriceAdapter) -> None:
    """登记一个持牌适配器（由采购落地后的部署步骤调用）。"""
    _REGISTERED[adapter.source_id] = adapter


def resolve_licensed_adapter(source_id: str) -> LicensedPriceAdapter:
    """取持牌适配器；未配置即拒绝，且**不回落到** seed/自抓源。"""
    adapter = _REGISTERED.get(source_id)
    if adapter is None:
        raise LicensedAdapterNotConfigured(
            f"licensed source {source_id!r} has no configured adapter "
            f"(registered: {', '.join(sorted(_REGISTERED)) or '<none>'}); refusing to "
            "fall back to a seed or self-crawl source"
        )
    return adapter


@dataclass(frozen=True)
class DeliveryAttribution:
    """对外交付必须携带的来源声明。"""

    source_id: str
    statement: str

    def to_dict(self) -> dict:
        return {"source_id": self.source_id, "statement": self.statement}


def required_attribution(reg: SourceRegistry, source_id: str) -> DeliveryAttribution | None:
    """某源对外交付时**必须**给出的声明；普通可交付源返回 ``None``。"""
    decl: SourceDeclaration = reg.get(source_id)
    if decl.license_state not in NOT_REPLACED_STATES:
        if decl.deliverable:
            return None
        raise DeliveryAttributionMissing(
            f"source {source_id!r} has license_state={decl.license_state!r} and is not "
            "deliverable at all"
        )
    return DeliveryAttribution(
        source_id=source_id,
        statement=(
            f"行情来源：{source_id}（临时源，待持牌源替换）；"
            "该序列仅用于内部研究，对外引用须同时声明来源与替换状态"
        ),
    )


def assert_delivery_allowed(
    reg: SourceRegistry,
    source_id: str,
    attribution: str | None,
) -> DeliveryAttribution | None:
    """对外交付前的合成闸：许可 + 声明。

    返回本次需要附带的声明（``None`` 表示无需声明）。不可交付或声明缺失时**抛错**。
    """
    required = required_attribution(reg, source_id)
    if required is None:
        return None
    if not attribution or not attribution.strip():
        raise DeliveryAttributionMissing(
            f"delivery references source {source_id!r} (temporary/provisional) without an "
            f"attribution statement; required statement: {required.statement!r}"
        )
    return required
