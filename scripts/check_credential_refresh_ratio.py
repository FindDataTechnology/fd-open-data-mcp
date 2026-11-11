#!/usr/bin/env python3
"""bound 凭据的刷新节奏必须与寿命成固定倍数（rotate-worker-cluster-credentials 3.3）。

规则只有一条：**刷新间隔 ≤ 寿命的 1/3**。

为什么是 1/3：留两次漏掉的机会。刷新在某天没跑，下一次还有 1/3 寿命可用；
再漏一次，仍剩 1/3。低于这个倍数，一次失败就开始逼近"某天派发突然 401"。

这个脚本不做任何集群操作，只做一件事：把"寿命"和"刷新间隔"两个数字从**各自的
真源**里读出来并核对——寿命在 `scripts/rotate_worker_credential.py`，间隔在
`.github/workflows/refresh-worker-credentials.yml` 的 cron 里。两边分居不同文件，
正是它们会漂的原因。

用法：
    python3 scripts/check_credential_refresh_ratio.py
    python3 scripts/check_credential_refresh_ratio.py --json

退出码：0 满足 / 1 违反（点名两个数值）/ 2 读不到其中一边（不猜）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ROTATE = REPO / "scripts" / "rotate_worker_credential.py"
WORKFLOW = REPO / ".github" / "workflows" / "refresh-worker-credentials.yml"

MAX_RATIO = 3  # 刷新间隔 × 3 ≤ 寿命


def read_duration_hours(path: Path | None = None) -> float | None:
    """寿命从轮换脚本的 `--duration` 默认值读。读不到就 None（不猜）。

    `path` 在**调用时**解析，不在定义时绑定：写成默认参数会在 import 那一刻固化，
    之后替换模块常量（测试、或将来支持 --rotate-script 覆盖）都不生效。
    """
    path = path or ROTATE
    m = re.search(r'"--duration",\s*default="(\d+)h"', path.read_text(encoding="utf-8"))
    return float(m.group(1)) if m else None


def read_interval_hours(path: Path | None = None) -> float | None:
    """刷新间隔从工作流的 cron 读。

    只认三种形态，其余一律 None（宁可 rc=2 也不猜）：
      `m */N * * *`  每 N 小时
      `m h * * *`    每天一次          -> 24h
      `m h * * D`    每周某天一次      -> 168h

    实现上先把「cron:」之后的整行取出来、去行尾注释、去引号，再按空格切字段。
    不用一条复杂正则去同时兼容带/不带引号和带注释：第一版就是因为只认到行尾，
    给 cron 加一句说明就解析失败（rc=2 噪声，很快会被 `|| true` 掉）；
    第二版用字符类把无引号的 `0 3 * * *` 只截到 `0`。两次都是"看起来能跑"。
    """
    path = path or WORKFLOW
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    m = re.search(r"^\s*-\s*cron:(.*)$", text, re.M)
    if not m:
        return None
    raw = m.group(1).strip()
    if raw[:1] in ("'", '"'):
        q = raw[0]
        end = raw.find(q, 1)
        if end == -1:
            return None
        raw = raw[1:end]
    else:
        raw = raw.split("#", 1)[0].strip()

    fields = raw.split()
    if len(fields) != 5:
        return None
    _minute, hour, dom, _month, dow = fields

    if hour.startswith("*/"):
        if dom != "*" or dow != "*":
            return None
        try:
            return 24.0 / float(hour[2:])
        except (ValueError, ZeroDivisionError):
            return None

    if hour.isdigit():
        if dom != "*":
            return None            # 月内某日：间隔不固定，不猜
        if dow == "*":
            return 24.0            # 每天
        if dow.isdigit():
            return 168.0           # 每周固定某天
        return None                # 1-5 / 1,3,5 之类：间隔不固定，不猜
    return None


def _rel(p: Path) -> str:
    """仓库内路径显示相对路径；测试里替换成 tmp 文件时退回绝对路径。"""
    try:
        return str(p.relative_to(REPO))
    except ValueError:
        return str(p)


def judge(duration_h: float, interval_h: float) -> dict:
    return {
        "duration_h": duration_h,
        "interval_h": interval_h,
        "ratio": (duration_h / interval_h) if interval_h else None,
        "max_ratio": MAX_RATIO,
        "ok": bool(interval_h) and interval_h * MAX_RATIO <= duration_h,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="核对 bound 凭据的刷新间隔 ≤ 寿命 1/3")
    ap.add_argument("--json", action="store_true")
    ns = ap.parse_args(argv)

    duration = read_duration_hours()
    interval = read_interval_hours()
    if duration is None:
        print(f"ERROR 读不到寿命：{ROTATE} 里没有 `--duration` 的 default", file=sys.stderr)
        return 2
    if interval is None:
        print(f"ERROR 读不到刷新间隔：{WORKFLOW} 不存在或 cron 形态无法解析",
              file=sys.stderr)
        return 2

    verdict = judge(duration, interval)
    if ns.json:
        print(json.dumps(verdict, ensure_ascii=False, indent=2))
        return 0 if verdict["ok"] else 1

    print(f"寿命     {duration:.0f}h（{duration / 24:.0f}d）  源：{_rel(ROTATE)}")
    print(f"刷新间隔 {interval:.0f}h（{interval / 24:.2f}d） 源：{_rel(WORKFLOW)}")
    print(f"倍数     寿命 / 间隔 = {verdict['ratio']:.1f}（要求 ≥ {MAX_RATIO}）")
    if verdict["ok"]:
        print("OK 刷新间隔不超过寿命的 1/3（允许连漏两次）")
        return 0
    print(f"FAIL 刷新间隔 {interval:.0f}h 过长：需 ≤ {duration / MAX_RATIO:.0f}h"
          f"（寿命 {duration:.0f}h 的 1/3）", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
