#!/usr/bin/env python3
"""worker 凭据面的双向巡检（rotate-worker-cluster-credentials 1.3）。

断言两个方向，任一处不成立即失败：

  1. **不该有的不出现**：`clusters.enabled=false`（或表里根本没有行）的集群，
     不得把凭据挂在控制面 pod 的 `/kubeconfigs` 上。挂着的 token 是可用凭据，
     禁用集群留一份等于给自己留后门。
  2. **该有的必须在**：`clusters.enabled=true` 的集群必须有 `<name>.token`，
     否则派发会在运行期才 401。

2026-11-11 的由来：清理泄漏 token 时发现 `fd-mcp/worker-kubeconfigs` 里躺着
america/baidu/tencent/zihan 四组凭据，而 `clusters` 表里只有 cheap 是 enabled；
其中 `zihan` 连表行都没有 —— 纯孤儿。这类残留没有任何运行期症状，只能靠巡检发现。

用法：
    # 在控制面 pod 内（有挂载 + 有 DB）
    python3 scripts/check_worker_credential_surface.py

    # 离线核对（不进集群）：用现成的目录与导出的集群表
    python3 scripts/check_worker_credential_surface.py \
        --kubeconfigs-dir ./fixtures/kubeconfigs --clusters-json ./fixtures/clusters.json

    # 只看不判（打印现状，退出码恒 0）
    python3 scripts/check_worker_credential_surface.py --report-only

退出码：0 通过 / 1 有违规（逐条点名）/ 2 无法取得判定所需输入。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

DEFAULT_KUBECONFIGS_DIR = "/kubeconfigs"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="巡检 worker 集群凭据与 clusters 表的一致性")
    p.add_argument(
        "--kubeconfigs-dir",
        default=DEFAULT_KUBECONFIGS_DIR,
        help=f"凭据挂载目录（默认 {DEFAULT_KUBECONFIGS_DIR}）",
    )
    p.add_argument(
        "--clusters-json",
        help="离线模式：clusters 表的 JSON 导出（[{name, enabled}, ...]）；不给则查库",
    )
    p.add_argument(
        "--db-url",
        help="数据库 DSN；不给则用 FD_OPEN_DATA_MCP_DATABASE_URL 环境变量",
    )
    p.add_argument(
        "--report-only",
        action="store_true",
        help="只打印现状，不做判定（退出码恒 0）",
    )
    return p.parse_args(argv)


def load_clusters(ns: argparse.Namespace) -> dict[str, bool]:
    """返回 {cluster_name: enabled}。取得不到就抛 RuntimeError（不静默当空）。"""
    if ns.clusters_json:
        raw = json.loads(Path(ns.clusters_json).read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise RuntimeError(f"{ns.clusters_json} 预期是数组，实际是 {type(raw).__name__}")
        out: dict[str, bool] = {}
        for row in raw:
            if not isinstance(row, dict) or "name" not in row:
                raise RuntimeError(f"{ns.clusters_json} 里有行缺少 name 字段：{row!r}")
            out[str(row["name"])] = bool(row.get("enabled", False))
        return out

    dsn = ns.db_url or os.environ.get("FD_OPEN_DATA_MCP_DATABASE_URL")
    if not dsn:
        raise RuntimeError(
            "拿不到 clusters 表：既没有 --clusters-json，也没有 --db-url / "
            "FD_OPEN_DATA_MCP_DATABASE_URL。不知道该信什么就不要给出判定。"
        )
    try:
        import psycopg2  # noqa: PLC0415  (只有在线模式才需要)
    except ImportError as exc:  # pragma: no cover - 环境相关
        raise RuntimeError(f"在线模式需要 psycopg2：{exc}") from exc

    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("select name, enabled from clusters order by name")
            return {str(name): bool(enabled) for name, enabled in cur.fetchall()}
    finally:
        conn.close()


def load_mounted_clusters(ns: argparse.Namespace) -> set[str]:
    """返回挂载目录里出现过的集群名（按 <name>.token 归并）。"""
    base = Path(ns.kubeconfigs_dir)
    if not base.is_dir():
        raise RuntimeError(
            f"凭据目录不存在：{base}。挂载缺失与「没有凭据」是两件事，不能混为一谈。"
        )
    names: set[str] = set()
    for entry in base.iterdir():
        if entry.name.endswith(".token") and entry.is_file():
            names.add(entry.name[: -len(".token")])
    return names


def check(clusters: dict[str, bool], mounted: set[str]) -> list[str]:
    """返回违规描述列表；空列表 = 通过。"""
    problems: list[str] = []

    for name in sorted(mounted):
        if name not in clusters:
            problems.append(
                f"{name}: 挂载里有凭据，但 clusters 表没有这个集群（孤儿凭据）"
            )
        elif not clusters[name]:
            problems.append(
                f"{name}: 挂载里有凭据，但该集群 enabled=false（应为重新启用时另签新凭据）"
            )

    for name in sorted(clusters):
        if clusters[name] and name not in mounted:
            problems.append(
                f"{name}: enabled=true 但挂载里没有 <name>.token（派发会在运行期 401）"
            )

    return problems


def main(argv: list[str] | None = None) -> int:
    ns = parse_args(argv)
    try:
        clusters = load_clusters(ns)
        mounted = load_mounted_clusters(ns)
    except RuntimeError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2

    print(f"clusters 表: {len(clusters)} 个（enabled={sum(1 for v in clusters.values() if v)}）")
    for name in sorted(clusters):
        print(f"  {name:<12} enabled={str(clusters[name]).lower():<5} "
              f"mounted={'yes' if name in mounted else 'no'}")
    orphans = sorted(mounted - set(clusters))
    if orphans:
        print(f"  挂载有、表里没有: {', '.join(orphans)}")

    if ns.report_only:
        print("REPORT-ONLY 未做判定")
        return 0

    problems = check(clusters, mounted)
    if not problems:
        print("OK 凭据面与 clusters 表一致")
        return 0

    print(f"FAIL {len(problems)} 处违规：", file=sys.stderr)
    for p in problems:
        print(f"  - {p}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
