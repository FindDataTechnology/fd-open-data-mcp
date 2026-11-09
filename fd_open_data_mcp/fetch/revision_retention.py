"""修订行的保留期清理（唯一允许删除 ``observation_revisions`` 的路径）。

为什么必须显式配置
------------------
修订行是 as-of 回放的唯一依据。删掉它，那个时点就永远读不出来了。所以：

* **没有默认 TTL**。不传 `older_than_days` 就不删任何东西——"忘了配"不该等于"开始删历史"。
* **默认 `dry_run=True`**。要看清楚会删多少、会失去什么，再决定。
* **`batch` 行默认不删**。整段修订行是"这段历史不可逐点回放"的**唯一证据**；删掉它，
  那一段在查询里就会退化成"没有修订记录"，读者再也看不出它被批量重写过。要删必须
  显式 `include_batch=True`，且调用方应同时登记该区间的不可回放性。

删除不会制造"假的精确"
----------------------
曾担心过：删掉修订后，某点会不会看起来"从未被修订过"从而被报成 `exact`？不会——
覆写会同时前推 `fetched_at`，而 `replayed_point` 对"无修订且 `fetched_at > 请求时点""
的点判 `absent`。所以删除的后果是**可用性下降**（原本能回放的时点变成 absent /
unavailable），不是把不确定伪装成确定。这个方向是可接受的降级；反过来（把不可知报成
已知）才是不能接受的。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

DEFAULT_BATCH_SIZE = 5000


def prune_revisions(
    session,
    *,
    older_than_days: Optional[int] = None,
    include_batch: bool = False,
    dry_run: bool = True,
    batch_size: int = DEFAULT_BATCH_SIZE,
    now: Optional[datetime] = None,
) -> dict:
    """按 ``revised_at`` 删除过旧的单点修订行。

    Input:
      older_than_days —— 保留窗口（天）。**必填才生效**；`None` 表示不清理任何东西。
      include_batch —— 是否连 `batch` 行一起删（默认否，见模块 docstring）。
      dry_run —— 默认 True，只统计不删。
      batch_size —— 单次 DELETE 的行数上限，避免长事务与长时间持锁。

    Returns: 字典，含
      cutoff —— 早于该时点的行被删；
      candidates —— {overwrite: n, batch: n}（batch 在 include_batch=False 时也会统计，
                   以便看清"被保护而未删"的量）；
      deleted —— 实际删除行数（dry_run 时为 0）；
      kept_batch —— 因 include_batch=False 而未删的 batch 行数；
      dry_run —— 回显本次是否只统计。
    """
    from sqlalchemy import func
    from fd_open_data_mcp.models import ObservationRevision

    result = {
        "cutoff": None,
        "candidates": {"overwrite": 0, "batch": 0},
        "deleted": 0,
        "kept_batch": 0,
        "dry_run": bool(dry_run),
        "enabled": older_than_days is not None,
    }
    if older_than_days is None:
        return result  # 未配置 = 不清理。绝不用默认值开始删历史。
    if older_than_days < 0:
        raise ValueError(f"older_than_days must be >= 0, got {older_than_days}")

    base_now = (now or datetime.now(timezone.utc)).replace(tzinfo=None)
    cutoff = base_now - timedelta(days=older_than_days)
    result["cutoff"] = cutoff.isoformat(sep=" ")

    counts = {
        kind: session.query(func.count(ObservationRevision.id))
        .filter(ObservationRevision.revised_at < cutoff,
                ObservationRevision.revision_kind == kind)
        .scalar()
        for kind in ("overwrite", "batch")
    }
    result["candidates"] = {"overwrite": int(counts["overwrite"]),
                            "batch": int(counts["batch"])}
    if not include_batch:
        result["kept_batch"] = int(counts["batch"])

    if dry_run:
        return result

    kinds = ["overwrite"] + (["batch"] if include_batch else [])
    total = 0
    while True:
        ids = [
            row[0] for row in
            session.query(ObservationRevision.id)
            .filter(ObservationRevision.revised_at < cutoff,
                    ObservationRevision.revision_kind.in_(kinds))
            .order_by(ObservationRevision.id)
            .limit(batch_size)
            .all()
        ]
        if not ids:
            break
        total += (
            session.query(ObservationRevision)
            .filter(ObservationRevision.id.in_(ids))
            .delete(synchronize_session=False)
        )
        session.commit()
    result["deleted"] = int(total)
    return result
