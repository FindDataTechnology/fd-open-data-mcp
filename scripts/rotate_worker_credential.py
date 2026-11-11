#!/usr/bin/env python3
"""轮换一个 worker 集群的派发凭据：签新 → 写回 → 在挂载点实测（change 2.x）。

为什么需要脚本：手工路径今天踩过四个坑，每一个都不会报错——

  · `kubectl get secret -o jsonpath={.data.cheap.token}` 把点当嵌套路径，读出空串
  · `kubectl patch` 报 `patched` 甚至 `(no change)`，值可能根本没变
  · Job `Complete` 在 `launched: []`（没活可派）时恒为真，看起来一切正常
  · 删掉 Secret 只是"暂停"，同名重建会把同一个 JWT 原样救活（legacy 是确定性签发）

因此本脚本的验收不是"命令返回 0"，而是**成对实测**：旧值必须被目标集群拒绝，
新值必须读通。任一条不满足即 `exit 1` 并打印实测响应。

两种签法（形态选择见 docs/adr/0004-worker-cluster-credential-lifecycle.md）：

  --mode bound    kubectl create token（带 exp，到期需再轮换）
  --mode legacy   删重建 SA 换 UID + 重建 token Secret（不可单独吊销）

用法：

    # 只打印计划（安全，默认不做任何改动）
    python3 scripts/rotate_worker_credential.py --cluster cheap \\
        --api-server https://100.64.0.13:6443 --dry-run

    # 只验当前状态（只读）
    python3 scripts/rotate_worker_credential.py --cluster cheap \\
        --api-server https://100.64.0.13:6443 --verify-only

    # 真跑（会改集群：旧凭据立即失效）
    python3 scripts/rotate_worker_credential.py --cluster cheap \\
        --api-server https://100.64.0.13:6443 --mode bound --apply

退出码：0 成功且成对验证通过 / 1 验证失败 / 2 输入或前置条件不满足。
"""

from __future__ import annotations

import argparse
import base64
import json
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request

CONTROL_CTX = "xinru"
SECRET = "worker-kubeconfigs"
SA = "crawl-worker"
SA_NAMESPACE = "scraw"
CONTROL_NAMESPACES = ("fd-master", "fd-mcp")
WORKER_CTX = {"cheap": "cheap-ts", "tencent": "tencent"}


def run(cmd: list[str], timeout: int = 90, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=stdin)


def read_control_token(namespace: str, cluster: str) -> str:
    """读控制面 Secret 里该集群的 token。走 JSON 解析，避开 jsonpath 点号坑。"""
    cp = run([
        "kubectl", "--context", CONTROL_CTX, "-n", namespace,
        "get", "secret", SECRET, "-o", "json",
    ])
    if cp.returncode != 0:
        raise RuntimeError(f"读 {namespace}/{SECRET} 失败：{cp.stderr.strip()[:200]}")
    data = json.loads(cp.stdout).get("data") or {}
    raw = data.get(f"{cluster}.token")
    return base64.b64decode(raw).decode().strip() if raw else ""


def read_control_ca(namespace: str, cluster: str) -> str | None:
    cp = run([
        "kubectl", "--context", CONTROL_CTX, "-n", namespace,
        "get", "secret", SECRET, "-o", "json",
    ])
    if cp.returncode != 0:
        return None
    raw = (json.loads(cp.stdout).get("data") or {}).get(f"{cluster}.ca")
    return base64.b64decode(raw).decode() if raw else None


def read_cluster_tls(cluster: str) -> tuple[str | None, bool]:
    """从该 worker 的 kubeconfig context 读 server 与 insecure 姿态。

    探测不该比集群自己更严：这些 worker 的 kubeconfig 普遍是
    `insecure-skip-tls-verify: true`（自签 CA），若探测强制校验证书，
    会把"证书链不被系统信任"误报成"凭据失效"。跟随既有姿态，并在输出里注明。
    """
    ctx = WORKER_CTX.get(cluster, cluster)
    cp = run(["kubectl", "config", "view", "--minify", "--context", ctx, "-o", "json"])
    if cp.returncode != 0:
        return None, False
    cl = (json.loads(cp.stdout).get("clusters") or [{}])[0].get("cluster") or {}
    return cl.get("server"), bool(cl.get("insecure-skip-tls-verify"))


def api_probe(
    api_server: str, token: str, ca_pem: str | None, namespace: str, insecure: bool = False
) -> tuple[bool, str]:
    """用给定 token 打一次 API。返回 (ok, 摘要)。不抛异常。

    显式禁用代理：本机若配了系统代理，urllib 会把 100.64/私有网段也送进代理，
    表现为 timeout 而不是 401 —— 那会被误读成"凭据失效"。
    """
    if insecure:
        ctx: ssl.SSLContext = ssl._create_unverified_context()  # noqa: SLF001
    elif ca_pem:
        ctx = ssl.create_default_context(cadata=ca_pem)
    else:
        ctx = ssl.create_default_context()
    url = f"{api_server}/api/v1/namespaces/{namespace}/pods?limit=1"
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=ctx),
    )
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with opener.open(req, timeout=20) as r:
            d = json.load(r)
        return True, f"OK {d.get('kind')} items={len(d.get('items', []))}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code} {e.reason}"
    except Exception as e:  # noqa: BLE001  - 网络类失败一律按"不可用"处理
        return False, f"{type(e).__name__}: {str(e)[:120]}"


def sign_bound(cluster: str, duration: str) -> str:
    ctx = WORKER_CTX.get(cluster, cluster)
    cp = run(["kubectl", "--context", ctx, "-n", SA_NAMESPACE,
              "create", "token", SA, f"--duration={duration}"])
    if cp.returncode != 0:
        raise RuntimeError(f"签 bound token 失败：{cp.stderr.strip()[:300]}")
    return cp.stdout.strip()


def sign_legacy(cluster: str) -> str:
    """换 SA 身份（UID 变）并重建 token Secret，返回新 token。"""
    ctx = WORKER_CTX.get(cluster, cluster)
    sa_manifest = (
        "apiVersion: v1\nkind: ServiceAccount\n"
        f"metadata:\n  name: {SA}\n  namespace: {SA_NAMESPACE}\n"
    )
    tok_manifest = (
        "apiVersion: v1\nkind: Secret\n"
        f"metadata:\n  name: {SA}-token\n  namespace: {SA_NAMESPACE}\n"
        f"  annotations:\n    kubernetes.io/service-account.name: {SA}\n"
        "type: kubernetes.io/service-account-token\n"
    )
    cp = run(["kubectl", "--context", ctx, "-n", SA_NAMESPACE, "delete", "sa", SA])
    if cp.returncode != 0 and "not found" not in (cp.stderr or "").lower():
        raise RuntimeError(f"删 SA 失败：{cp.stderr.strip()[:300]}")
    for name, body in (("ServiceAccount", sa_manifest), ("token Secret", tok_manifest)):
        cp = run(["kubectl", "--context", ctx, "apply", "-f", "-"], stdin=body)
        if cp.returncode != 0:
            raise RuntimeError(f"重建 {name} 失败：{cp.stderr.strip()[:300]}")
    for _ in range(20):
        cp = run(["kubectl", "--context", ctx, "-n", SA_NAMESPACE,
                  "get", "secret", f"{SA}-token", "-o", "json"])
        if cp.returncode == 0:
            raw = (json.loads(cp.stdout).get("data") or {}).get("token")
            if raw:
                return base64.b64decode(raw).decode().strip()
        time.sleep(1)
    raise RuntimeError("token Secret 创建后 20s 内未被控制器填充")


def write_back(cluster: str, token: str) -> list[str]:
    """写回两个控制面命名空间，并**读回校验**（防空操作）。"""
    b = base64.b64encode(token.encode()).decode()
    patch = json.dumps({"data": {f"{cluster}.token": b}})
    problems: list[str] = []
    for ns in CONTROL_NAMESPACES:
        cp = run(["kubectl", "--context", CONTROL_CTX, "-n", ns, "patch", "secret",
                  SECRET, "--type=merge", "-p", patch])
        if cp.returncode != 0:
            problems.append(f"{ns}: patch 失败 {cp.stderr.strip()[:150]}")
            continue
        got = read_control_token(ns, cluster)
        if got != token.strip():
            problems.append(f"{ns}: patch 报成功但读回不一致（空操作）")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="轮换并实测一个 worker 集群的派发凭据")
    ap.add_argument("--cluster", required=True, help="worker 集群名（如 cheap）")
    ap.add_argument("--api-server", help="目标集群 API；不给则从该集群的 kubeconfig context 取")
    ap.add_argument("--mode", choices=("bound", "legacy"), default="bound")
    ap.add_argument("--duration", default="720h", help="bound token 寿命（默认 720h）")
    ap.add_argument("--namespace", default=SA_NAMESPACE, help="派发目标命名空间")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不改任何东西")
    ap.add_argument("--verify-only", action="store_true", help="只验当前值可用")
    ap.add_argument("--apply", action="store_true", help="真跑（旧凭据会立即失效）")
    ns = ap.parse_args(argv)

    if ns.dry_run or not (ns.verify_only or ns.apply):
        ctx = WORKER_CTX.get(ns.cluster, ns.cluster)
        print(f"计划 cluster={ns.cluster} mode={ns.mode}")
        if ns.mode == "bound":
            print(f"  1. kubectl --context {ctx} -n {SA_NAMESPACE} create token {SA} --duration={ns.duration}")
        else:
            print(f"  1. kubectl --context {ctx} -n {SA_NAMESPACE} delete sa {SA}")
            print(f"  2. kubectl --context {ctx} apply -f -   # ServiceAccount/{SA}")
            print(f"  3. kubectl --context {ctx} apply -f -   # Secret/{SA}-token")
        for n in CONTROL_NAMESPACES:
            print(f"  ·  kubectl --context {CONTROL_CTX} -n {n} patch secret {SECRET} --type=merge -p <{ns.cluster}.token>")
        print("  ·  成对实测：旧值必须失败，新值必须在挂载点返回 PodList")
        if not ns.apply:
            print("DRY-RUN 未执行（加 --apply 才真跑）")
            return 0
        print()

    old = read_control_token(CONTROL_NAMESPACES[0], ns.cluster)
    ca = read_control_ca(CONTROL_NAMESPACES[0], ns.cluster)
    cfg_server, insecure = read_cluster_tls(ns.cluster)
    api = ns.api_server or cfg_server
    if not api:
        print("ERROR 拿不到 API 地址：--api-server 未给，kubeconfig 里也读不到", file=sys.stderr)
        return 2
    if insecure:
        print("提示：该集群 kubeconfig 为 insecure-skip-tls-verify，探测沿用同一姿态")

    if ns.verify_only:
        if not old:
            print(f"ERROR 控制面没有 {ns.cluster}.token", file=sys.stderr)
            return 2
        ok, detail = api_probe(api, old, ca, ns.namespace, insecure)
        print(f"当前值实测：{detail}")
        return 0 if ok else 1

    try:
        new = sign_bound(ns.cluster, ns.duration) if ns.mode == "bound" else sign_legacy(ns.cluster)
    except RuntimeError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 2
    if not new:
        print("ERROR 签出的 token 为空", file=sys.stderr)
        return 2

    problems = write_back(ns.cluster, new)
    if problems:
        for p in problems:
            print(f"ERROR {p}", file=sys.stderr)
        return 1

    new_ok, new_detail = api_probe(api, new, ca, ns.namespace, insecure)
    print(f"新值实测：{new_detail}")
    if not new_ok:
        return 1

    if old:
        old_ok, old_detail = api_probe(api, old, ca, ns.namespace, insecure)
        print(f"旧值实测（期望失败）：{old_detail}")
        if old_ok:
            print("ERROR 旧值仍然可用——轮换未生效", file=sys.stderr)
            return 1
    else:
        print("旧值实测：无旧值（首次签发），跳过成对判定")

    if old and old.strip() == new.strip():
        print("ERROR 新旧值相同——签出来的不是新凭据", file=sys.stderr)
        return 1

    print("OK 轮换完成且成对验证通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
