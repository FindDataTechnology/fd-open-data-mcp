#!/usr/bin/env python3
"""到期前的主动告警（rotate-worker-cluster-credentials 3.2 的"眼睛"落地版）。

`check_worker_credential_surface.py` 能算出"剩余寿命已低于 1/3"，但**没人会每天去跑它**。
这个脚本把那个判断接到集群里已有的告警通道上（`visibility.notifiers` → wechat/ServerChan，
凭据在 `crawl-watcher-secret`），于是 bound token 快到期时是**推送**而不是等着某天派发 401。

判定本身不在这里，也不在隔壁脚本里：它在包内
`fd_open_data_mcp.visibility.credentials`，`visibility.scan` 用的是同一份。
这条"剩余 < 寿命 1/3"若有两份实现，漂移后告警说健康、巡检说违规，没人分得清谁对。

**本脚本是过渡形态，会被 scan 取代（已定，见 tasks.md §3.2）。**
`visibility.scan` 每 15 分钟跑一次、自己就有去重和推送通道，把这条检查折进去
之后这条独立 CronJob 就是多余的。之所以先并存：新代码要到 0.5.62 才进镜像，
在那之前删掉它就是**拆掉已有的安全网**。上线后跑一次 `kubectl delete cronjob`
再删本文件即可。

用法（在挂载了 `/kubeconfigs` 的 pod 里）：
    python3 alert_stale_credentials.py                # 有问题才推送，退出码 1
    python3 alert_stale_credentials.py --dry-run      # 只打印，不推送

退出码：0 全部健康 / 1 有到期风险（**已尝试推送**）/ 2 读不到凭据目录。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# `sys.path[0]` 是脚本所在目录（scripts/），所以裸 `import fd_open_data_mcp` 会去解析
# **环境里装的那个包**——CI runner 上常常根本没有（没跑 pip install），开发机上则可能是
# 另一个检出。repo 根在 sys.path 里时本仓版本优先，脚本才和它所在的代码一致。
# 在 ConfigMap 副本场景（/opt/credential-alert/）下 parents[1] 是 `/`，不满足条件，
# 于是照旧解析镜像里的包——两种部署都得到正确结果。
_ROOT = Path(__file__).resolve().parents[1]
if (_ROOT / "fd_open_data_mcp").is_dir() and str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    from fd_open_data_mcp.visibility.credentials import (
        DEFAULT_KUBECONFIGS_DIR,
        describe_lifetime,
        lifetime_problems,
        read_lifetimes,
    )
except ImportError as exc:  # 镜像里的包比脚本旧：说清楚，别让它变成一个裸 traceback
    print(f"ERROR 当前的 fd_open_data_mcp 缺少 visibility.credentials（{exc}）。\n"
          f"      本脚本需要 fd-open-data-mcp >= 0.5.62。在镜像更新前**不要**重新生成\n"
          f"      ConfigMap credential-alert-scripts——在跑的旧 Job 会因此变成 ImportError，\n"
          f"      也就是把到期告警本身弄哑。", file=sys.stderr)
    raise SystemExit(2) from exc

DEFAULT_DIR = DEFAULT_KUBECONFIGS_DIR


def scan(kubeconfigs_dir: str) -> tuple[dict[str, dict], list[str]]:
    """读挂载目录并给出问题清单。目录不存在 → FileNotFoundError（不是"健康"）。"""
    lifetimes = read_lifetimes(kubeconfigs_dir)
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
