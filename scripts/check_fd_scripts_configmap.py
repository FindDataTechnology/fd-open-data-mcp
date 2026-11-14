#!/usr/bin/env python3
"""配置在集群里的**策略脚本副本**，与仓里那份是不是同一份？

## 为什么需要它

`executor=direct` 的策略不跑镜像里的脚本，而是把源码**从 ConfigMap 挂进 Job**
（`reconciler._direct_manifests` → `_read_script`）。搜索链是
`/app/scripts`（镜像）→ `/opt/fd-scripts`（ConfigMap `fd-scripts`）→ `FD_SCRIPTS_DIR`，
**镜像优先**。所以：

- 同一个文件名，镜像里那份永远赢——集群改了 ConfigMap 也不生效；
- 想用 ConfigMap 那份，只能**换个名字**（2026-11-14 就是这么做的：策略改成
  `bulk_ingest_astock_daily_v2`，ConfigMap 里放 `bulk_ingest_astock_daily_v2.py`）。

这条别名因此成了**生产日更策略的唯一代码来源**，而它此前没有任何出处校验：
`fd-scripts` 不是 Argo 管的、仓里没有清单定义它、`check_inline_configmap_copies.py`
只校验**声明过** `finddata.io/inline-sources` 的 ConfigMap（它没标）。后果与
「主机裸拷贝」「内联副本分叉」同族：**谁改了仓里那份，线上不会跟着变，而两边各自都绿**。

## 判据

逐个键比字节：ConfigMap 的 `<key>` ↔ `git show <ref>:scripts/<path>`。
`--map` 用来表达别名（`_v2` 那份的真源是非 v2 的文件名）。不一致时报字节数与
首个不同行号，并给两条改法。**取不到集群或仓 → 判不出（退 2）**：缺数不是一致。

    2026-11-15 实测（这条闸的由来）：`bulk_ingest_astock_daily_v2.py` 与
    `origin/main:scripts/bulk_ingest_astock_daily.py` sha256 同为 `9581c0dd…`
    （1309 行、0 行差异）——**当时是一致的**，所以这道闸是回归守卫，不是缺陷报告。

## 用法

    python scripts/check_fd_scripts_configmap.py            # 缺省：比 astock 两个键
    python scripts/check_fd_scripts_configmap.py --all      # 比 ConfigMap 里所有 .py
    python scripts/check_fd_scripts_configmap.py --self-test
    python scripts/check_fd_scripts_configmap.py --namespace fd-master --context xinru

只读：只 `kubectl get cm` 与 `git show`，不写盘、不 apply。
"""
from __future__ import annotations

import argparse
import subprocess
import sys

#: ConfigMap 键 → 仓内路径。别自动推断：`_v2` 这种别名与真源文件名不同，
#: 推断出来的映射会在改名当天静默指向一个不存在的文件（然后「找不到」被读成「一致」）。
DEFAULT_MAP = {
    "bulk_ingest_astock_daily.py": "scripts/bulk_ingest_astock_daily.py",
    "bulk_ingest_astock_daily_v2.py": "scripts/bulk_ingest_astock_daily.py",
    "bulk_ingest_fund_nav_daily.py": "scripts/bulk_ingest_fund_nav_daily.py",
    "bulk_ingest_financials_aggregate.py": "scripts/bulk_ingest_financials_aggregate.py",
    "bulk_ingest_stock_financials.py": "scripts/bulk_ingest_stock_financials.py",
    "bulk_ingest_stock_indicators_events.py": "scripts/bulk_ingest_stock_indicators_events.py",
    "run_astock_backfill_batches.py": "scripts/run_astock_backfill_batches.py",
}


def _sh(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout


def _cm_data(context: str, namespace: str, name: str) -> dict[str, str]:
    """整份 data 一次取回（`-o go-template` 逐键取要 N 次 kubectl）。"""
    import json

    raw = _sh(["kubectl", "--context", context, "-n", namespace,
               "get", "cm", name, "-o", "json"])
    return json.loads(raw).get("data") or {}


def _first_diff(a: str, b: str) -> tuple[int, str, str]:
    al, bl = a.splitlines(), b.splitlines()
    for i in range(min(len(al), len(bl))):
        if al[i] != bl[i]:
            return i + 1, al[i], bl[i]
    if len(al) != len(bl):
        i = min(len(al), len(bl))
        return i + 1, (al[i] if i < len(al) else "<无此行>"), (bl[i] if i < len(bl) else "<无此行>")
    return 0, "", ""


def compare(cm: dict[str, str], repo: dict[str, str], keys: list[str],
            repo_text: dict[str, str | None]) -> list[dict]:
    """纯函数：逐键比字节。`repo_text` 是 {仓内路径: 内容}（取不到 ⇒ None）。

    纯函数是刻意的：这道闸的判据（一致/分叉/缺键/判不出）要能在没有集群、
    没有仓的情况下被自证，否则「全都判红」与「判据写反」在输出上同形。
    """
    out: list[dict] = []
    for key in keys:
        path = repo.get(key)
        if path is None:
            continue                      # 没声明映射的键不猜（见 DEFAULT_MAP 注释）
        if key not in cm:
            out.append({"key": key, "path": path, "verdict": "missing_in_cluster",
                        "detail": "ConfigMap 里没有这个键（策略引用了它 ⇒ 启动会报 script not found）"})
            continue
        got = cm[key]
        want = repo_text.get(path)
        if want is None:
            out.append({"key": key, "path": path, "verdict": "undetermined",
                        "detail": f"仓里取不到 {path}"})
            continue
        if got.rstrip("\n") == want.rstrip("\n"):
            out.append({"key": key, "path": path, "verdict": "same",
                        "detail": f"{len(want.encode())} B / {len(want.splitlines())} 行"})
            continue
        line, ca, cb = _first_diff(got, want.rstrip("\n"))
        out.append({"key": key, "path": path, "verdict": "diverged",
                    "detail": f"集群 {len(got.encode())} B vs 仓内 {len(want.encode())} B；"
                              f"首个不同在第 {line} 行\n        集群: {ca[:110]}\n"
                              f"        仓内: {cb[:110]}"})
    return out


def _self_test() -> int:
    """三档各一条（一致 / 分叉 / 集群缺键）+ 一条「仓里没有就不猜」。"""
    repo_text = {
        "scripts/a.py": "x = 1\ny = 2\n",
        "scripts/b.py": "x = 1\ny = 2\n",
        "scripts/c.py": "x = 9\n",
    }
    ok = True

    def check(label, got, want):
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'✓' if good else '✗'} {label}: got={got} want={want}")

    cases = compare({"a.py": "x = 1\ny = 2\n", "b.py": "x = 1\ny = 3\n"},
                    {"a.py": "scripts/a.py", "b.py": "scripts/b.py", "c.py": "scripts/c.py"},
                    ["a.py", "b.py", "c.py", "nosuch.py"], repo_text)
    got = {c["key"]: c["verdict"] for c in cases}
    check("一致", got.get("a.py"), "same")
    check("分叉", got.get("b.py"), "diverged")
    check("集群缺键", got.get("c.py"), "missing_in_cluster")
    check("未声明的键不猜", "nosuch.py" in got, False)
    # 尾部换行不算差异（内联副本的常态写法）
    tail = compare({"a.py": "x = 1\ny = 2"}, {"a.py": "scripts/a.py"}, ["a.py"], repo_text)
    check("只差尾部换行 = 一致", tail[0]["verdict"], "same")
    # 仓里取不到 ⇒ 判不出，不许读成一致
    und = compare({"a.py": "x\n"}, {"a.py": "scripts/a.py"}, ["a.py"], {})
    check("仓里没有 ⇒ 判不出", und[0]["verdict"], "undetermined")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--context", default="xinru", help="kubectl context（reconciler 在 xinru）")
    ap.add_argument("--namespace", default="fd-master")
    ap.add_argument("--configmap", default="fd-scripts")
    ap.add_argument("--ref", default="origin/main",
                    help="比对的 git ref（该仓主工作树常停在别人分支上，默认读远端树）")
    ap.add_argument("--repo", default=None, help="仓根（缺省=脚本所在仓）")
    ap.add_argument("--all", action="store_true", help="比 ConfigMap 里所有已声明映射的 .py 键")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()

    import os

    repo = args.repo or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        cm = _cm_data(args.context, args.namespace, args.configmap)
    except Exception as exc:  # noqa: BLE001 - 判不出必须退 2
        print(f"判不出（EXIT=2）：取不到 {args.namespace}/{args.configmap}：{exc}",
              file=sys.stderr)
        return 2
    if not cm:
        print(f"判不出（EXIT=2）：{args.namespace}/{args.configmap} 的 data 为空", file=sys.stderr)
        return 2

    keys = [k for k in cm if k.endswith(".py")] if args.all else list(DEFAULT_MAP)
    repo_text: dict[str, str | None] = {}
    for key in keys:
        path = DEFAULT_MAP.get(key)
        if path is None:
            continue
        try:
            repo_text[path] = _sh(["git", "-C", repo, "show", f"{args.ref}:{path}"])
        except subprocess.CalledProcessError:
            repo_text[path] = None

    results = compare(cm, DEFAULT_MAP, keys, repo_text)
    bad = [r for r in results if r["verdict"] in ("diverged", "missing_in_cluster")]
    und = [r for r in results if r["verdict"] == "undetermined"]
    print(f"{args.namespace}/{args.configmap} 对 {args.ref}（{len(results)} 个键）：")
    marks = {"same": "✓", "diverged": "✗", "missing_in_cluster": "✗", "undetermined": "?"}
    for r in results:
        print(f"{marks[r['verdict']]} {r['key']} ↔ {r['path']}: {r['detail']}")
    if bad:
        print("\n改法二选一：① 改仓里那份（它是真源），再把集群 ConfigMap 同步成它；"
              "② 先改集群而不同步仓 —— 下次谁从仓里重建就静默回退。")
    if und:
        print(f"\n判不出 {len(und)} 个键（仓里 {args.ref} 没有对应文件）——缺数不是一致。")
        return 2
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
