"""worker 集群凭据的寿命判定（rotate-worker-cluster-credentials 3.2）。

**这是唯一一份判定实现。** 它原先长在 `scripts/check_worker_credential_surface.py`
里，于是同一条阈值（"剩余 < 寿命 1/3"）被两处各写一遍——巡检脚本一处、告警 CronJob
一处。这个 change 一路在批的就是这种双真理：两边一旦漂移，告警说健康、巡检说违规，
而没人分得清谁对。

搬进包里的另一个理由很实际：`python -m fd_open_data_mcp.visibility.scan` 在运行时
只装得到包，装不到 `scripts/`（那是 sdist 里的数据文件，不在 sys.path 上）。想让
scan 自己带上这条检查，判定就必须在包里。

判据只来自 token 自身（JWT 的 `iat`/`exp`），不靠外部记账：
- bound（有 exp）：会到期。到期是**静默**的——派发某天突然 401，之前毫无征兆。
- legacy（JWT 但无 exp）：永不失效。"看不出寿命"本身就是它最大的风险。
- unknown（不是可解 JWT）：不知道，就不能假装知道。

`stale = 剩余 < 寿命 / 3`。这与 3.3 要求的刷新间隔（≤ 寿命 1/3）是同一个刻度的两端：
刷新正常时它永远不亮，刷新停了它先亮，且留出的跑道正好够一轮刷新周期。
"""
from __future__ import annotations

import base64
import json
import time
from collections.abc import Iterable, Mapping
from pathlib import Path

# 寿命判定的唯一阈值。改这里，巡检脚本与 scan 告警同时改——这就是搬进包里的目的。
STALE_FRACTION = 1.0 / 3.0

# 控制面 pod 里凭据的挂载点（Secret `worker-kubeconfigs`，键为 `<cluster>.token`）。
DEFAULT_KUBECONFIGS_DIR = "/kubeconfigs"


def decode_jwt_payload(token: str) -> dict | None:
    """读 JWT 的 claims，**不验签**。

    只用来回答"它什么时候过期"。验签是 API server 的事；这里验了也没意义，
    因为真要证明凭据有效只有打一次 API（见 `rotate_worker_credential.py`）。
    """
    parts = token.strip().split(".")
    if len(parts) != 3:
        return None
    body = parts[1]
    pad = "=" * (-len(body) % 4)
    try:
        raw = base64.urlsafe_b64decode(body + pad)
        claims = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return None
    return claims if isinstance(claims, dict) else None


def credential_lifetime(token: str, now: float | None = None) -> dict:
    """返回 {kind, issued_at, expires_at, lifetime_s, remaining_s, stale}。

    kind: bound（有 exp）/ legacy（JWT 但无 exp）/ unknown（不是可解 JWT）。
    """
    claims = decode_jwt_payload(token)
    if claims is None:
        return {"kind": "unknown", "issued_at": None, "expires_at": None,
                "lifetime_s": None, "remaining_s": None, "stale": None}
    iat = claims.get("iat")
    exp = claims.get("exp")
    if exp is None:
        # 无 exp 的 JWT = legacy SA token：永不过期。这不是"健康"，是"没有兜底"。
        return {"kind": "legacy", "issued_at": iat, "expires_at": None,
                "lifetime_s": None, "remaining_s": None, "stale": None}

    now = time.time() if now is None else now
    lifetime = (exp - iat) if isinstance(iat, (int, float)) else None
    remaining = exp - now
    stale = bool(lifetime and remaining < lifetime * STALE_FRACTION)
    return {"kind": "bound", "issued_at": iat, "expires_at": exp,
            "lifetime_s": lifetime, "remaining_s": remaining, "stale": stale}


def fmt_seconds(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)
    if seconds >= 86400:
        return f"{sign}{seconds / 86400:.1f}d"
    if seconds >= 3600:
        return f"{sign}{seconds / 3600:.1f}h"
    return f"{sign}{seconds / 60:.0f}m"


def describe_lifetime(life: dict) -> str:
    if life["kind"] == "unknown":
        return "寿命未知（token 不是可解 JWT）"
    if life["kind"] == "legacy":
        return "永不过期（legacy，无 exp）"
    life_s = fmt_seconds(life["lifetime_s"])
    rem = fmt_seconds(life["remaining_s"])
    flag = "  STALE" if life["stale"] else ""
    if life["remaining_s"] is not None and life["remaining_s"] < 0:
        flag = "  EXPIRED"
    return f"bound 寿命 {life_s}，剩余 {rem}{flag}"


def lifetime_problem_for(name: str, life: dict) -> str | None:
    """单个集群的问题描述，没有则 None。

    **这是那条阈值的唯一落点**：`lifetime_problems`（巡检脚本）和
    `visibility.scan`（集群内告警）都调它。谁再写一遍 `remaining < lifetime/3`，
    就又是两份真值了。
    """
    if life["kind"] != "bound":
        return None  # legacy 是选择、unknown 是不知道——都不算违规
    rem = life["remaining_s"]
    if rem is not None and rem < 0:
        return f"{name}: bound token 已过期（派发此刻就会 401）——刷新没在跑"
    if life["stale"]:
        return (f"{name}: bound token 剩余 {fmt_seconds(rem)} 已低于寿命的 1/3"
                f"（刷新没在跑，或刷新间隔 > 寿命 1/3）")
    return None


def lifetime_problems(lifetimes: dict[str, dict]) -> list[str]:
    """到期/刷新停摆算违规；legacy 不算违规（它是选择，不是故障）。"""
    out = (lifetime_problem_for(name, lifetimes[name]) for name in sorted(lifetimes))
    return [p for p in out if p]


def read_lifetimes(kubeconfigs_dir: str | Path) -> dict[str, dict]:
    """读 `<dir>/<cluster>.token`，返回 {cluster: lifetime}。

    目录不存在 → 抛 FileNotFoundError。**"没挂载"与"没有凭据"是两件事**，
    调用方要分开处理（scan 选择静默跳过，巡检脚本选择判失败）。
    """
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
    return lifetimes


def mounted_clusters(kubeconfigs_dir: str | Path) -> set[str]:
    """挂载目录里出现过的集群名（按 `<name>.token` 归并）。

    目录不存在 → 抛 FileNotFoundError，与 `read_lifetimes` 同一姿态：由调用方决定
    "静默跳过"还是"判失败"，不在这里把"没挂载"与"没有凭据"抹平。

    只读文件名、不解析内容：派发会在**运行期**才因为缺凭据而 401，这里要在那之前
    就能回答"该有的在不在"。
    """
    base = Path(kubeconfigs_dir)
    if not base.is_dir():
        raise FileNotFoundError(f"凭据目录不存在：{base}")
    return {entry.name[: -len(".token")] for entry in base.iterdir()
            if entry.name.endswith(".token") and entry.is_file()}


def surface_problems(clusters: Mapping[str, bool],
                     mounted: Iterable[str]) -> list[dict]:
    """挂载面与 `clusters` 表的一致性。空列表 = 一致。

    **这是唯一一份判定**：离线巡检脚本要逐条打印、集群内 scan 要逐条告警并去重，
    两处都调它。否则又是两份真值——一边说违规、一边说没事，没人分得清谁对。

    三种形态：
    - ``orphan``    表里没有这个集群（谁都不知道它属于谁）
    - ``disabled``  `enabled=false` 却有凭据（禁用集群留一把可用凭据＝给自己留后门）
    - ``missing``   `enabled=true` 却没有凭据（派发要到运行期才 401）
    """
    mounted_set = set(mounted)
    out: list[dict] = []
    for name in sorted(mounted_set):
        if name not in clusters:
            out.append({
                "cluster": name, "kind": "orphan",
                "problem": f"{name}: 挂载里有凭据，但 clusters 表没有这个集群（孤儿凭据）",
            })
        elif not clusters[name]:
            out.append({
                "cluster": name, "kind": "disabled",
                "problem": (f"{name}: 挂载里有凭据，但该集群 enabled=false"
                            "（应为重新启用时另签新凭据）"),
            })
    for name in sorted(clusters):
        if clusters[name] and name not in mounted_set:
            out.append({
                "cluster": name, "kind": "missing",
                "problem": (f"{name}: enabled=true 但挂载里没有 <name>.token"
                            "（派发会在运行期 401）"),
            })
    return out
