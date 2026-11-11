#!/usr/bin/env python3
"""到期前的主动告警（rotate-worker-cluster-credentials 3.2 的"眼睛"落地版）。

`check_worker_credential_surface.py` 能算出"剩余寿命已低于 1/3"，但**没人会每天去跑它**。
这个脚本把那个判断接到集群里已有的告警通道上（`visibility.notifiers` → wechat/ServerChan，
凭据在 `crawl-watcher-secret`），于是 bound token 快到期时是**推送**而不是等着某天派发 401。

为什么不复用 `credential_lifetime()` 的实现：它就在隔壁
`check_worker_credential_surface.py` 里，同一目录，直接 import——两处各写一份"剩余 < 寿命 1/3"
正是这个 change 一路在批的那种双真理。

用法（在挂载了 `/kubeconfigs` 的 pod 里）：
    python3 alert_stale_credentials.py                # 有问题才推送，退出码 1
    python3 alert_stale_credentials.py --dry-run      # 只打印，不推送

退出码：0 全部健康 / 1 有到期风险（**已尝试推送**）/ 2 读不到凭据目录。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 同目录的兄弟模块：脚本目录本身就是 sys.path[0]，所以直接 import 即可。
from check_worker_credential_surface import (  # noqa: E402
    credential_lifetime,
    describe_lifetime,
    lifetime_problems,
)

DEFAULT_DIR = "/kubeconfigs"


def scan(kubeconfigs_dir: str) -> tuple[dict[str, dict], list[str]]:
    base = Path(kubeconfigs_dir)
    if not base.is_dir():
        raise FileNotFoundError(f"凭据目录不存在：{base}")
    lifetimes: dict[str, dict] = {}
    for entry in sorted(base.iterdir()):
        if not (entry.name.endswith(".token") and entry.is_file()):
            continue
        name = entry.name[: -len(".token")]
        try:
            lifetimes[name] = credential_lifetime(entry.read_text(encoding="utf-8"))
        except OSError as exc:  # 读不到也是信息，不能当成"健康"
            lifetimes[name] = {"kind": "unknown", "issued_at": None, "expires_at": None,
                               "lifetime_s": None, "remaining_s": None, "stale": None,
                               "error": str(exc)}
    return lifetimes, lifetime_problems(lifetimes)


def render(lifetimes: dict[str, dict], problems: list[str]) -> tuple[str, str]:
    title = f"worker 凭据需要刷新（{len(problems)} 项）"
    lines = ["以下 bound 凭据已进入需要刷新的区间（剩余 < 寿命 1/3 或已过期）：", ""]
    lines += [f"- {p}" for p in problems]
    lines += ["", "当前各集群："]
    for name in sorted(lifetimes):
        lines.append(f"- {name}: {describe_lifetime(lifetimes[name])}")
    lines += ["", "处理：python3 scripts/rotate_worker_credential.py --cluster <name> "
                  "--mode bound --apply（或等 CI 的 refresh-worker-credentials 跑）"]
    return title, "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="bound 凭据到期前的主动告警")
    ap.add_argument("--kubeconfigs-dir", default=DEFAULT_DIR)
    ap.add_argument("--dry-run", action="store_true", help="只打印，不推送")
    ns = ap.parse_args(argv)

    try:
        lifetimes, problems = scan(ns.kubeconfigs_dir)
    except FileNotFoundError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2

    for name in sorted(lifetimes):
        print(f"{name}: {describe_lifetime(lifetimes[name])}")
    if not problems:
        print("OK 没有到期风险")
        return 0

    title, body = render(lifetimes, problems)
    print(f"ALERT {title}")
    print(body)
    if ns.dry_run:
        print("DRY-RUN 未推送")
        return 1

    # 复用已有的投递抽象：它在缺 token / 网络失败时只记日志、不抛异常，
    # 所以告警通道坏掉不会把这条 Job 变成另一种假红。
    from fd_open_data_mcp.visibility.notifiers.factory import get_notifier

    get_notifier().send(title, body, level="error")
    return 1


if __name__ == "__main__":
    sys.exit(main())
