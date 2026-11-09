#!/usr/bin/env python3
"""接入声明巡检（只读）。change: define-ingest-revision-and-coverage 任务 3.2/3.3。

零写入：不建文件、不改 manifest、不碰数据库。

    # 只看声明（默认；对 162 个 spider 跑）
    python3 scripts/inspect_ingest_declarations.py --manifests ../fd-industry-data/spiders

    # 机器可读
    python3 scripts/inspect_ingest_declarations.py --manifests ... --json

退出码：0 = 没有非法声明；1 = 存在非法声明值（拼错等）；2 = 用法错误。
"待补"不导致非零退出——它不是一个错误，是一个待办清单。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fd_open_data_mcp.ingest_declarations import (  # noqa: E402
    COVERAGE_CAPABILITIES,
    REVISION_POLICIES,
    classify_paths,
    glob_manifests,
    inspect,
    load_declarations,
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ingest declaration inspection (read-only)")
    ap.add_argument("--manifests", required=True,
                    help="spiders 目录（读取 <dir>/*/manifest.yaml）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--show-pending", type=int, default=10,
                    help="打印前 N 个待补源（默认 10；0 = 不打印）")
    ap.add_argument("--classify", action="store_true",
                    help="按可观测信号给存量源分桶并给出建议声明（只建议，不写回）")
    args = ap.parse_args(argv)

    root = Path(args.manifests)
    if not root.is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 2

    paths = glob_manifests(root)
    if not paths:
        print(f"error: no manifest.yaml found under {root}", file=sys.stderr)
        return 2

    if args.classify:
        cls = classify_paths(paths)
        if args.json:
            print(json.dumps(cls, ensure_ascii=False, indent=2))
        else:
            print(f"manifests : {cls['total']}")
            for bucket, count in cls["counts"].items():
                sample = ", ".join(x["name"] for x in cls["buckets"][bucket][:5])
                print(f"  {bucket:14}: {count:3}   {sample}")
            print(f"\n{cls['note']}")
        return 0

    report = inspect(load_declarations(paths))

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        c = report["counts"]
        print(f"manifests   : {report['total']}")
        print(f"已声明      : {c['declared']}")
        print(f"待补        : {c['pending']}")
        print(f"非法值      : {c['invalid']}")
        print(f"允许取值    : revision_policy={list(REVISION_POLICIES)} "
              f"coverage_capability={list(COVERAGE_CAPABILITIES)}")
        if args.show_pending and report["pending"]:
            print("\n待补（前 N 个，不批量填假声明）：")
            for d in report["pending"][:args.show_pending]:
                print(f"  - {d['name']}: 缺 {', '.join(d['missing_fields'])}")
        if report["invalid"]:
            print("\n非法声明值（拼错即等于没声明）：")
            for d in report["invalid"]:
                for k, v in d["invalid"].items():
                    print(f"  - {d['name']}: {k}={v!r}")

    return 1 if report["invalid"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
