"""Daily digest 的状态分桶必须与总数对得上（unblock-silent-dispatch-stalls）。

背景：`snapshot.recent_runs()` **不过滤状态**（`order_by(started_at).limit(n)`），
所以每个终态都会进 `recent_runs`。而 `format_digest()` 的 YESTERDAY 行曾经只数
success / failed / zero_yield / redundant / no_op 五种 —— 2026-11-13 引入的
`stalled`（回收器关掉的悬行：派发没成、产出未知）与 `skipped`（单飞跳过留痕）
都落在桶外：总数照样含它们，各项之和就是补不平，而且差的那几条不带名字。

约束（三条断言，全部对修复前代码为红）：
1. `stalled` 有自己的一桶，且数量正确 —— 这是最该被看见的一档；
2. `skipped` 有自己的一桶（跳过留痕不是"没跑过"）；
3. 出现词表之外的任何状态时，差额按**状态名**念出来，不静默丢弃。
"""
from __future__ import annotations

from zoneinfo import ZoneInfo

from fd_open_data_mcp.visibility.digest import format_digest


def _snap(statuses):
    return {
        "generated_at": "2026-11-15T00:00:00Z",
        "recent_runs": [
            {"policy": f"p{i}", "status": st, "started_at": "2026-11-14T10:00:00Z"}
            for i, st in enumerate(statuses)
        ],
        "summary": {"stale_run_count": 0},
        "per_source_outcome": [],
        "circuit_state": [],
        "fleet_yield": {"rows_new": 0, "rows_attempted": 0, "runs": len(statuses)},
        "fleet": [],
        "today_scheduled": [],
    }


def _yesterday_line(statuses):
    _, body = format_digest(_snap(statuses), ZoneInfo("Asia/Shanghai"))
    return next(ln for ln in body.splitlines() if ln.startswith("YESTERDAY"))


def test_digest_counts_stalled_run():
    """stalled 必须自己占一桶 —— 它此前既在总数里、又不在任何桶里。"""
    line = _yesterday_line(["success", "stalled"])
    assert "2 runs" in line
    assert "1 ⏳" in line, line


def test_digest_counts_skipped_run():
    """skipped 是"被挡住"的留痕，不是"没跑过"，日报里要看得见。"""
    line = _yesterday_line(["skipped", "skipped"])
    assert "2 ⤼" in line, line


def test_digest_buckets_reconcile_with_total():
    """分桶之和 == 总数（词表内全部状态），差额为零时不印 ❓ 段。"""
    statuses = ["success", "failed", "stalled", "zero_yield",
                "redundant", "no_op", "skipped"]
    line = _yesterday_line(statuses)
    assert "7 runs" in line
    assert "❓" not in line, line
    for bit in ("1 ✅", "1 ❌", "1 ⏳", "1 0️⃣", "1 ♻️", "1 ⏭️", "1 ⤼"):
        assert bit in line, (bit, line)


def test_digest_names_unaccounted_status():
    """词表外的状态按名字念出来 —— 新终态只会让这段变长，不会让数字对不上。"""
    line = _yesterday_line(["success", "cancelled"])
    assert "2 runs" in line
    assert "❓ 1 (cancelled)" in line, line
