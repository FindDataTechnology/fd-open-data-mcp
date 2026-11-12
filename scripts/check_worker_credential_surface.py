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

# `sys.path[0]` 是脚本所在目录（scripts/），裸 `import fd_open_data_mcp` 会解析
# **环境里装的那个包**：CI runner 上常常没有（工作流只 checkout、不 pip install），
# 开发机上则可能指向另一个检出。把 repo 根放进 sys.path 首位，脚本才和它所在的
# 代码一致；挂载副本场景（ConfigMap/镜像里拷出来的脚本）下 parents[1] 是 `/`，
# 不满足条件，照旧用镜像里的包。
_ROOT = Path(__file__).resolve().parents[1]
if (_ROOT / "fd_open_data_mcp").is_dir() and str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# 寿命判定住在包里（`fd_open_data_mcp.visibility.credentials`），因为
# `python -m ...visibility.scan` 在运行时只装得到包、装不到 scripts/。
# 这里**只 import，不复制**——同一条"剩余 < 寿命 1/3"出现两份，就是这个
# change 一路在批的双真理：漂移后告警说健康、巡检说违规，没人分得清谁对。
try:
    from fd_open_data_mcp.visibility.credentials import (
        DEFAULT_KUBECONFIGS_DIR,
        credential_lifetime,
        describe_lifetime,
        lifetime_problems,
    )
except ImportError as exc:  # 镜像/环境里的包比脚本旧：点名说清楚，退出码 2
    print(f"ERROR 当前的 fd_open_data_mcp 缺少 visibility.credentials（{exc}）。\n"
          f"      本脚本需要 fd-open-data-mcp >= 0.5.62；在镜像/环境更新前不要把它装进\n"
          f"      任何巡检 Job 或 ConfigMap 副本（那份副本会用旧包判出'健康'）。",
          file=sys.stderr)
    raise SystemExit(2) from exc


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
    p.add_argument(
        "--json",
        action="store_true",
        help="机器可读输出（含每个集群的凭据剩余寿命），供看板/告警消费",
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


# ─── 凭据剩余寿命（rotate-worker-cluster-credentials 3.2）────────────────────
# 判定实现已搬进包里（`fd_open_data_mcp.visibility.credentials`），因为
# `python -m ...visibility.scan` 在运行时只装得到包、装不到 scripts/。
# 这里**只 import，不复制**——同一条"剩余 < 寿命 1/3"出现两份，就是这个
# change 一路在批的双真理：漂移后告警说健康、巡检说违规，没人分得清谁对。
from fd_open_data_mcp.visibility.credentials import (  # noqa: E402
    DEFAULT_KUBECONFIGS_DIR,
    credential_lifetime,
    describe_lifetime,
    lifetime_problems,
    surface_problems,
)


def check(clusters: dict[str, bool], mounted: set[str]) -> list[str]:
    """返回违规描述列表；空列表 = 通过。

    判定本身在包里（`credentials.surface_problems`）：集群内 `visibility.scan` 每 15
    分钟也要判同一件事并告警，两处必须是同一份实现——否则一边说违规、一边说没事，
    而分不清谁对正是这类检查最容易失效的方式。这里只负责把记录摊平成可打印的行。
    """
    return [p["problem"] for p in surface_problems(clusters, mounted)]



def main(argv: list[str] | None = None) -> int:
    ns = parse_args(argv)
    try:
        clusters = load_clusters(ns)
        mounted = load_mounted_clusters(ns)
    except RuntimeError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2

    base = Path(ns.kubeconfigs_dir)
    lifetimes: dict[str, dict] = {}
    for name in sorted(mounted):
        try:
            token = (base / f"{name}.token").read_text(encoding="utf-8")
        except OSError as exc:
            print(f"ERROR 读不到 {name}.token：{exc}", file=sys.stderr)
            return 2
        lifetimes[name] = credential_lifetime(token)

    problems = check(clusters, mounted) + lifetime_problems(lifetimes)
    ok = not problems

    if ns.json:
        print(json.dumps({
            "ok": ok or ns.report_only,
            "clusters": [
                {"name": n, "enabled": clusters[n], "mounted": n in mounted,
                 "credential": lifetimes.get(n)}
                for n in sorted(clusters)
            ],
            "orphans": sorted(mounted - set(clusters)),
            "problems": problems,
        }, ensure_ascii=False, indent=2))
        if ns.report_only:
            return 0
        return 0 if ok else 1

    print(f"clusters 表: {len(clusters)} 个（enabled={sum(1 for v in clusters.values() if v)}）")
    for name in sorted(clusters):
        life = lifetimes.get(name)
        print(f"  {name:<12} enabled={str(clusters[name]).lower():<5} "
              f"mounted={'yes' if name in mounted else 'no':<3} "
              f"{describe_lifetime(life) if life else '挂载里没有凭据'}")
    orphans = sorted(mounted - set(clusters))
    if orphans:
        print(f"  挂载有、表里没有: {', '.join(orphans)}")

    if ns.report_only:
        print("REPORT-ONLY 未做判定")
        return 0

    if ok:
        print("OK 凭据面与 clusters 表一致，且没有到期/刷新的问题")
        return 0

    print(f"FAIL {len(problems)} 处违规：", file=sys.stderr)
    for p in problems:
        print(f"  - {p}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
