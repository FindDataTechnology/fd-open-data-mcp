"""留存契约的解析与写入闸门（`establish-data-retention-principle` R1/R2）。

原则一句话：**留存由"能否再取"决定，不由"是否重要"决定**。本模块负责让它在写入路径
上真的生效——否则契约只是审计报告里的一列字。

三档行为（`FD_RETENTION_GATE`）
------------------------------
* ``off``（**默认**）：完全不检查。生产部署里没有这个开关时行为与从前一致。
* ``warn``：查契约、缺了只在日志里报警，**仍允许写入**。
* ``enforce``：缺契约即拒绝写入，抛 :class:`RetentionContractMissing`。

为什么默认是 ``off`` 而不是 ``enforce``：存量有 72 个 (源, 族) 尚未声明契约（合计仅
6MB，都是历史小源）。直接 enforce 会让这些源的爬取**当晚全线停摆**——那不是"更严格"，
那是拿生产事故换一条规则。正确顺序是：先 seed 契约（`scripts/seed_retention_contracts.py`）
→ 观察 warn 一段时间 → 再开 enforce。

`family` 的取法
---------------
概念码的第一段（`financials.revenue` → `financials`）。这与 `concepts.code` 的既有约定
一致，也是审计器与实测分类用的同一口径——两处不一致会造出"审计说合规、闸门说违规"。
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

#: 未声明契约时拒绝写入（仅在 enforce 档）。
class RetentionContractMissing(Exception):
    def __init__(self, message: str, *, source: str | None, family: str | None):
        super().__init__(message)
        self.source = source
        self.family = family


def gate_mode() -> str:
    """当前档位：off / warn / enforce。非法值按 off 处理并报警——但绝不猜测更严。"""
    raw = (os.environ.get("FD_RETENTION_GATE") or "off").strip().lower()
    if raw in ("off", "warn", "enforce"):
        return raw
    logger.warning("未知 FD_RETENTION_GATE=%r，按 off 处理", raw)
    return "off"


def family_of(code: str | None) -> str | None:
    """概念码 → 概念族（第一段）。`financials.revenue` → `financials`。"""
    if not code:
        return None
    return str(code).split(".", 1)[0]


def resolve_contract(session, source: str | None, family: str | None):
    """按 (source, family) 取契约：精确 → 该源 `*` → 无。

    返回 ORM 行或 ``None``。``None`` 的含义是**未声明**，不是"默认合规"。
    结果按 session 记忆，避免每写一行都查一次库。
    """
    from fd_open_data_mcp.models import DataRetentionContract

    s = source or "(null)"
    f = family or "(null)"
    memo = session.info.setdefault("_retention_contract_memo", {})
    if (s, f) in memo:
        return memo[(s, f)]

    row = (session.query(DataRetentionContract)
           .filter(DataRetentionContract.source == s,
                   DataRetentionContract.family == f).one_or_none())
    if row is None:
        row = (session.query(DataRetentionContract)
               .filter(DataRetentionContract.source == s,
                       DataRetentionContract.family == "*").one_or_none())
    memo[(s, f)] = row
    return row


def check_write_allowed(session, *, concept_id: int | None, source: str | None) -> None:
    """写入前的准入检查。**只读**，不改任何东西。

    在 ``off`` 档直接返回（零开销）；``warn`` 档记日志；``enforce`` 档缺契约即抛。
    """
    mode = gate_mode()
    if mode == "off":
        return

    family = None
    if concept_id is not None:
        from fd_open_data_mcp.models import Concept

        concept = session.get(Concept, concept_id)
        family = family_of(getattr(concept, "code", None) if concept else None)

    contract = resolve_contract(session, source, family)
    if contract is not None and contract.retention_class:
        return

    detail = (f"concept_id={concept_id} source={source!r} family={family!r} "
              f"→ 没有留存契约（P/F/W/T 未声明）")
    if mode == "enforce":
        raise RetentionContractMissing(
            f"拒绝写入：{detail}。先在 data_retention_contracts 声明契约。",
            source=source, family=family,
        )
    logger.warning("留存契约缺失（warn 档，仍写入）：%s", detail)
