#!/usr/bin/env python3
"""指数日线种子导入 CLI（add-price-supply-and-caliber task 5.2）。

一次性通道：把一份已有的指数日线快照（CSV）导入联邦视图，``source_used=index-seed``。
不抓取、不联网、不改任何既有源的行。

CSV 表头：``date,symbol,open,high,low,close,volume,amount,name``（字段可缺，``date`` 与
``symbol`` 必填，``close`` 强烈建议——没有 close 的指数点无法复核）。

    python scripts/import_index_seed.py seed.csv --dry-run
    python scripts/import_index_seed.py seed.csv
    python scripts/import_index_seed.py seed.csv --source index-seed

退出码：0 全部成功；1 有行被拒（其余照常写入）；2 跑不起来（文件/库/概念族问题）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Import index daily bars as a provisional seed")
    ap.add_argument("csv", help="CSV snapshot (date,symbol,open,high,low,close,volume,amount,name)")
    ap.add_argument("--source", default="index-seed", help="source_used value (default: index-seed)")
    ap.add_argument("--dry-run", action="store_true", help="validate + report, write nothing")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    try:
        from fd_open_data_mcp.db import get_database
        from fd_open_data_mcp.price_sources import import_index_seed, read_seed_csv

        rows = read_seed_csv(args.csv)
    except Exception as exc:
        print(f"[fatal] {exc}", file=sys.stderr)
        return 2

    if not rows:
        print(f"[fatal] {args.csv}: no data rows", file=sys.stderr)
        return 2

    session = get_database().get_session()
    try:
        report = import_index_seed(session, rows, source_id=args.source, dry_run=args.dry_run)
    except Exception as exc:
        print(f"[fatal] import failed: {exc}", file=sys.stderr)
        return 2
    finally:
        session.close()

    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        verb = "would write" if report.dry_run else "wrote"
        print(
            f"{verb} {report.observations_written} observations "
            f"({report.observations_existing} already present) from {report.rows_in} rows; "
            f"rejected {len(report.rejected)}"
        )
        if report.entities_created:
            print(f"  entities created: {', '.join(report.entities_created)}")
        if report.concepts_created:
            print(f"  concepts created: {', '.join(report.concepts_created)}")
        for r in report.rejected[:10]:
            print(f"  REJECTED row {r['row']}: {r['reason']}")

    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
