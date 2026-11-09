#!/usr/bin/env python3
"""把接入声明写回 spider manifest（decision 4A）。只读默认，`--apply` 才写。

规则（照 4A 的"逐桶复核"执行）
--------------------------
* 只写 `price_like` 与 `period_series` 两桶（本轮 136 个）。
* `undetermined` 一律不动——它们没有 `data_period` 也没有价格概念，写一个值就是猜。
* 已有声明且值不同的 manifest **拒绝改写**，列出来给人看（不做静默覆盖）。
* 幂等：两个键都已存在则跳过。

声明值的选择依据（逐桶复核结论，2026-11-09）
------------------------------------------
* `revision_policy: overwrite_with_history` —— 这些源的修订是逐点的（CPI 事后修订、
  大宗价格更正），而 `write_cache_range` 自本轮起会保留旧值。`batch_revision` 只
  适用于"上游整段重写"的前复权股票序列，本仓当前没有。
* `coverage_capability: gaps_only` —— 覆盖度是我们**服务层**算出来的（`read_series` /
  `read_range_with_coverage` 都从本仓序列日期推导），不是上游提供的能力；`full`
  必须由读面探针证明，不能靠这个脚本断言。

    python3 scripts/apply_ingest_declarations.py --manifests <dir>            # dry-run
    python3 scripts/apply_ingest_declarations.py --manifests <dir> --apply    # 写入
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fd_open_data_mcp.ingest_declarations import (  # noqa: E402
    _load_yaml,
    classify_manifest,
    glob_manifests,
)

WRITABLE_BUCKETS = ("price_like", "period_series")
DECLARATIONS = {
    "revision_policy": "overwrite_with_history",
    "coverage_capability": "gaps_only",
}
COMMENT = "# 接入声明（change: define-ingest-revision-and-coverage, decision 4A）"


@dataclass
class Outcome:
    path: Path
    name: str
    action: str          # written | skipped_present | refused_conflict | skipped_bucket
    detail: str = ""


def apply_one(path: Path) -> Outcome:
    data = _load_yaml(path)
    name = str(data.get("name") or path.parent.name)

    info = classify_manifest(data)
    if info["bucket"] not in WRITABLE_BUCKETS:
        return Outcome(path, name, "skipped_bucket",
                       f"bucket={info['bucket']}（人工读，不猜）")

    conflicts = {
        k: data.get(k) for k, v in DECLARATIONS.items()
        if data.get(k) is not None and data.get(k) != v
    }
    if conflicts:
        return Outcome(path, name, "refused_conflict", str(conflicts))

    if all(data.get(k) == v for k, v in DECLARATIONS.items()):
        return Outcome(path, name, "skipped_present")

    return Outcome(path, name, "written", "")


def write_one(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    if not text.endswith("\n"):
        text += "\n"
    block = COMMENT + "\n" + "".join(f"{k}: {v}\n" for k, v in DECLARATIONS.items())
    path.write_text(text + block, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="apply ingest declarations to spider manifests")
    ap.add_argument("--manifests", required=True)
    ap.add_argument("--apply", action="store_true", help="真正写入（默认 dry-run）")
    args = ap.parse_args(argv)

    root = Path(args.manifests)
    if not root.is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 2

    outcomes = [apply_one(p) for p in glob_manifests(root)]
    counts: dict[str, int] = {}
    for o in outcomes:
        counts[o.action] = counts.get(o.action, 0) + 1

    if args.apply:
        for o in outcomes:
            if o.action == "written":
                write_one(o.path)
                # 写完立刻回读：写坏 yaml 比没写更糟
                check = _load_yaml(o.path)
                assert check.get("revision_policy") == DECLARATIONS["revision_policy"]
                assert check.get("coverage_capability") == DECLARATIONS["coverage_capability"]

    print(f"{'APPLIED' if args.apply else 'DRY-RUN'}  manifests={len(outcomes)}")
    for action in ("written", "skipped_present", "skipped_bucket", "refused_conflict"):
        print(f"  {action:18} {counts.get(action, 0)}")
    for o in outcomes:
        if o.action == "refused_conflict":
            print(f"  ! {o.name}: 已有不同声明 {o.detail} — 未改写，需人工定")
    return 1 if counts.get("refused_conflict") else 0


if __name__ == "__main__":
    raise SystemExit(main())
