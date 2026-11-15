#!/usr/bin/env python3
"""每条 `executor=direct` 的策略，**实际会跑哪一份脚本字节**？

## 为什么需要它（与 check_fd_scripts_configmap.py 的分工）

`reconciler._read_script` 的搜索链是

    /app/scripts（镜像） → /opt/fd-scripts（ConfigMap `fd-scripts`） → FD_SCRIPTS_DIR

**镜像优先、按 basename 匹配**。于是同一份脚本存在多个候选档，只有**第一个命中**
会被挂进 Job。`check_fd_scripts_configmap.py` 比的是「ConfigMap ↔ 仓」，它对
**镜像那一档完全瞎**：2026-11-15 实测镜像 `sha-2f2ccd2` 里
`bulk_ingest_astock_daily.py` 是 **52336 B / 无 tencent**，而仓内已是 62864 B，
这个陈旧既不报错也不告警。如果谁把策略的 `script` 从 `_v2` 改回基名（退役别名
时最自然的动作），生产就静默回退到那份旧脚本，而两道闸各自都绿。

`_read_script` 自己在**运行时**会对「两份不同」打 WARNING（2026-11-11 加的），
但它只看得见**正在解析的那个名字**，而且日志没人读、事后也难反查。这道闸把同一
件事在**离线**、**逐策略**、**可回归**的位置再说一遍。

## 判据

对每条**启用**的 direct 策略：

1. 按链序解析出 effective（第一档命中）；
2. 与 `<ref>:scripts/<path>` 逐字节比（别名映射见 `DEFAULT_MAP`，别推断）；
3. 三档结论：`ok` / `stale_effective`（跑的不是仓里那份 ⇒ 命中）/
   `missing`（三档都没有 ⇒ 启动即 FileNotFoundError ⇒ 命中）；
4. 顺带报一条**事实**：低优先档里也放着同一文件但字节不同 ⇒ `inert_lower_copy`
   ——「改那一档不会生效」。这条不命中（今天两者可以都是对的），但它是上一个
   坑的形状，必须点名。
5. 停用的策略只报事实不计命中（它现在跑不了）；仓里取不到 ⇒ 判不出（退 2）。

## 别名什么时候才能退役（第六档输出）

`_v2` 这类别名存在的唯一理由是绕开镜像遮蔽，所以**它必然只活在 ConfigMap 一档**：
按策略的 basename 去量，镜像档永远「没有这个文件」。于是「镜像里那份基名脚本
更新了没有」这个退役前置条件，在这道闸原来的口径里**结构性看不见**——2026-11-15
想退役别名时，闸给的是 `✓ ok`，它答的是「别名现在好用」，而不是「可以不用了」。

所以探针额外按 **canonical basename**（`DEFAULT_MAP[script]` 的文件名）取各档指纹，
再算一条 `alias_retirement`：

* 三档都没有 canonical ⇒ 退役后启动即 FileNotFoundError（不可退役）；
* 退役后第一个命中的那档字节 == 仓内 ⇒ **可退役**（改名后照样跑仓里那份）；
* 否则 ⇒ **暂不可退役**，点名是哪一档的旧字节在遮蔽（通常是镜像，等出片）。

这条只是**事实**、不计命中：今天别名工作正常，这个结论回答的是「下一步能不能动」。
把它算成红会让整道闸在「等在出片」的正常状态下常红。

## 用法

    python scripts/check_policy_script_resolution.py                 # 探针在集群里跑
    python scripts/check_policy_script_resolution.py --probe-json f  # 用已有探针产物（离线）
    python scripts/check_policy_script_resolution.py --self-test

`--probe` 的做法：在 fd-master 起一个一次性 pod，用**同一个镜像**、挂**同一份
ConfigMap**，在里面查 `crawl_policies` 并把每档的 (bytes, sha256) 打成一行 JSON。
镜像档只有进镜像才看得见——这是它不能退化成纯 `kubectl get cm` 的原因。

只读：探针只 `select`、只读文件；本脚本不写盘、不 apply。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys

#: 策略里的 `script` 名 → 仓内路径。**别自动推断**：`_v2` 是绕开镜像遮蔽的别名，
#: 它的真源是非 v2 那个文件；推断出来的映射会在改名当天指向不存在的文件，而
#: 「找不到」看起来跟「一致」一样安静。
DEFAULT_MAP = {
    "bulk_ingest_astock_daily": "scripts/bulk_ingest_astock_daily.py",
    "bulk_ingest_astock_daily_v2": "scripts/bulk_ingest_astock_daily.py",
    "bulk_ingest_fund_nav_daily": "scripts/bulk_ingest_fund_nav_daily.py",
    "bulk_ingest_financials_aggregate": "scripts/bulk_ingest_financials_aggregate.py",
    "bulk_ingest_stock_financials": "scripts/bulk_ingest_stock_financials.py",
    "bulk_ingest_stock_indicators_events": "scripts/bulk_ingest_stock_indicators_events.py",
    "run_astock_backfill_batches": "scripts/run_astock_backfill_batches.py",
}

TIERS = ["/app/scripts", "/opt/fd-scripts"]

#: 探针源码：在集群里跑，产出「策略 + 各档文件指纹」一行 JSON。
#: 单独常量是为了能被 `--self-test` 编译检查（拼错的 f-string 只有真跑才炸）。
PROBE_SRC = r'''
import os, json, hashlib
from sqlalchemy import create_engine, text

TIERS = ["/app/scripts", "/opt/fd-scripts", os.environ.get("FD_SCRIPTS_DIR", "")]
engine = create_engine(os.environ["FD_OPEN_DATA_MCP_DATABASE_URL"])
with engine.connect() as conn:
    rows = list(conn.execute(text(
        "select name, script, enabled from crawl_policies "
        "where executor='direct' and script is not null order by name"
    )))
policies = [{"name": n, "script": s, "enabled": bool(e)} for n, s, e in rows]

# 除了每条策略自己的 basename，再按调用方给的 canonical 名字取指纹。
# 别名（`_v2`）只在 ConfigMap 一档存在 ⇒ 只按策略名取，永远量不到镜像里那份
# 基名脚本，而「它更新了没有」正是别名能不能退役的唯一前置条件。
extra = [x.strip() for x in os.environ.get("FD_PROBE_NAMES", "").split(",") if x.strip()]
names = sorted({os.path.basename(p["script"]) + ".py" for p in policies} | set(extra))

files = {}
for base in names:
    for tier in TIERS:
        if not tier:
            continue
        path = os.path.join(tier, base)
        if not os.path.isfile(path):
            continue
        blob = open(path, "rb").read()
        files.setdefault(base, {})[tier] = {
            "bytes": len(blob),
            "sha": hashlib.sha256(blob).hexdigest(),
        }
print("PROBE_JSON " + json.dumps({"policies": policies, "files": files}))
'''


def resolve(policies: list[dict], files: dict[str, dict[str, dict]],
            repo: dict[str, str | None], mapping: dict[str, str]) -> list[dict]:
    """纯函数：逐策略算 effective 与结论。

    `files` 是 {basename: {tier: {"bytes": n, "sha": hex}}}，`repo` 是
    {仓内路径: sha256 或 None}（取不到 ⇒ None）。纯函数是刻意的：三档结论必须
    能在没有集群、没有仓的情况下自证，否则「全都判红」与「判据写反」同形。
    """
    order = [t for t in TIERS]
    out: list[dict] = []
    for policy in policies:
        script = policy["script"]
        base = os.path.basename(script) + ".py"
        present = files.get(base) or {}
        winner_tier = next((t for t in order if t in present), None)
        entry = {"policy": policy["name"], "script": script,
                 "enabled": bool(policy.get("enabled")), "basename": base}
        if winner_tier is None:
            entry.update(verdict="missing",
                         detail=f"三档（{'、'.join(order)}）都没有 {base} —— "
                                f"启动时 _read_script 会抛 FileNotFoundError")
            out.append(entry)
            continue
        win = present[winner_tier]
        path = mapping.get(script)
        if path is None:
            entry.update(verdict="undetermined",
                         detail=f"策略引用的 script={script!r} 不在 DEFAULT_MAP 里 "
                                f"——不知道对应仓内哪份，不猜")
            out.append(entry)
            continue
        want = repo.get(path)
        if want is None:
            entry.update(verdict="undetermined",
                         detail=f"仓里取不到 {path}")
            out.append(entry)
            continue
        # 低优先档里也放着同一文件、但字节不同 ⇒ 改那一档不会生效（事实，不命中）
        inert = [{"tier": t, "bytes": present[t]["bytes"]}
                 for t in order if t in present and t != winner_tier
                 and present[t]["sha"] != win["sha"]]
        entry["inert_lower_copy"] = inert
        if win["sha"] == want:
            entry.update(verdict="ok",
                         detail=f"effective={winner_tier} {win['bytes']} B，与仓内一致")
        else:
            entry.update(verdict="stale_effective",
                         detail=f"effective={winner_tier} {win['bytes']} B sha={win['sha'][:16]} "
                                f"≠ 仓内 {path} sha={want[:16]} —— **跑的不是仓里那份**")
        out.append(entry)
    return out


def alias_retirement(results: list[dict], files: dict[str, dict[str, dict]],
                     repo: dict[str, str | None],
                     mapping: dict[str, str]) -> list[dict]:
    """别名（`_v2`）现在能不能退役？——纯函数，与 `resolve` 同款可离线自证。

    只看**别名的策略**（`DEFAULT_MAP[script]` 的文件名 ≠ 策略自己的 basename）。
    判据是「退役后第一个命中的档，字节是否等于仓内」：退役＝把策略的 `script`
    改回基名，此后解析链从镜像那档重新开始，遮蔽会不会回来完全取决于那里。

    `retirable` 三态：True / False / None（仓里取不到，判不出——不猜）。
    """
    out: list[dict] = []
    for r in results:
        path = mapping.get(r["script"])
        if not path:
            continue
        canonical = os.path.basename(path)
        if canonical == r["basename"]:
            continue                      # 不是别名
        present = files.get(canonical) or {}
        tier = next((t for t in TIERS if t in present), None)
        want = repo.get(path)
        entry = {"policy": r["policy"], "alias": r["basename"],
                 "canonical": canonical}
        if want is None:
            entry.update(retirable=None,
                         detail=f"仓里取不到 {path}，不判")
        elif tier is None:
            entry.update(retirable=False,
                         detail=f"三档都没有 {canonical} ——退役别名后 _read_script "
                                f"会抛 FileNotFoundError")
        elif present[tier]["sha"] == want:
            entry.update(retirable=True,
                         detail=f"退役后 effective={tier} {present[tier]['bytes']} B，"
                                f"与仓内一致")
        else:
            entry.update(retirable=False,
                         detail=f"退役后 effective={tier} {present[tier]['bytes']} B "
                                f"sha={present[tier]['sha'][:16]} 是旧字节"
                                f"（≠ 仓内 {want[:16]}）——那档仍在遮蔽，先出片")
        out.append(entry)
    return out


def _self_test() -> int:
    """十一条：一致／跑错档／三档全缺／仓里没有／低档惰性副本／停用只报事实／
    别名映射到基名／档序（镜像赢）／别名退役三态。"""
    mapping = {"a": "scripts/a.py", "a_v2": "scripts/a.py", "b": "scripts/b.py",
               "b_v2": "scripts/b.py", "c": "scripts/c.py", "d": "scripts/d.py",
               "e_v2": "scripts/e.py"}
    sha = lambda s: hashlib.sha256(s.encode()).hexdigest()  # noqa: E731
    repo = {"scripts/a.py": sha("new"), "scripts/b.py": sha("b"),
            "scripts/c.py": sha("c"), "scripts/e.py": sha("e")}
    files = {
        "a.py": {"/app/scripts": {"bytes": 3, "sha": sha("old")},
                 "/opt/fd-scripts": {"bytes": 3, "sha": sha("new")}},
        # 两档同字节（都是仓里那份）⇒ ok 且不该报惰性副本
        "b.py": {"/app/scripts": {"bytes": 1, "sha": sha("b")},
                 "/opt/fd-scripts": {"bytes": 1, "sha": sha("b")}},
        "a_v2.py": {"/opt/fd-scripts": {"bytes": 3, "sha": sha("new")}},
        "b_v2.py": {"/opt/fd-scripts": {"bytes": 1, "sha": sha("b")}},
        "e_v2.py": {"/opt/fd-scripts": {"bytes": 1, "sha": sha("e")}},
        "c.py": {"/app/scripts": {"bytes": 1, "sha": sha("c")}},
        "d.py": {"/app/scripts": {"bytes": 1, "sha": sha("d")}},
    }
    policies = [
        {"name": "p-ok-alias", "script": "a_v2", "enabled": True},
        {"name": "p-stale-image", "script": "a", "enabled": True},
        {"name": "p-missing", "script": "zzz", "enabled": True},
        {"name": "p-undet", "script": "d", "enabled": True},
        {"name": "p-both-same", "script": "b", "enabled": True},
        {"name": "p-c-off", "script": "c", "enabled": False},
        {"name": "p-alias-retirable", "script": "b_v2", "enabled": True},
        {"name": "p-alias-blocked", "script": "e_v2", "enabled": True},
    ]
    got = {r["policy"]: r for r in resolve(policies, files, repo, mapping)}
    ret = {a["policy"]: a for a in alias_retirement(
        list(got.values()), files, repo, mapping)}
    ok = True

    def check(label, cond, extra=""):
        nonlocal ok
        ok = ok and bool(cond)
        print(f"  {'✓' if cond else '✗'} {label}{'' if cond else f'  ← {extra}'}")

    check("别名只在 ConfigMap 一档 ⇒ ok", got["p-ok-alias"]["verdict"] == "ok",
          got["p-ok-alias"]["verdict"])
    check("镜像档陈旧而仓里已新 ⇒ stale_effective",
          got["p-stale-image"]["verdict"] == "stale_effective",
          got["p-stale-image"]["verdict"])
    check("低档惰性副本被点名（ConfigMap 那份改了不生效）",
          any(i["tier"] == "/opt/fd-scripts"
              for i in got["p-stale-image"].get("inert_lower_copy") or []),
          got["p-stale-image"].get("inert_lower_copy"))
    check("三档全缺 ⇒ missing", got["p-missing"]["verdict"] == "missing")
    check("仓里取不到 ⇒ undetermined", got["p-undet"]["verdict"] == "undetermined",
          got["p-undet"]["verdict"])
    check("两档同字节 ⇒ ok 且无惰性副本",
          got["p-both-same"]["verdict"] == "ok"
          and not got["p-both-same"]["inert_lower_copy"])
    check("停用策略只报事实（仍给结论但不计命中，由 main 过滤）",
          got["p-c-off"]["verdict"] == "ok" and got["p-c-off"]["enabled"] is False)
    check("档序：镜像赢（即便 ConfigMap 更新）",
          got["p-stale-image"]["detail"].startswith("effective=/app/scripts"))
    check("别名退役：基名仍是镜像旧字节 ⇒ 不可退役",
          ret["p-ok-alias"]["retirable"] is False
          and "/app/scripts" in ret["p-ok-alias"]["detail"],
          ret["p-ok-alias"])
    check("别名退役：退役后命中的档与仓一致 ⇒ 可退役",
          ret["p-alias-retirable"]["retirable"] is True,
          ret["p-alias-retirable"])
    check("别名退役：三档都没有基名 ⇒ 不可退役且点名 FileNotFoundError",
          ret["p-alias-blocked"]["retirable"] is False
          and "FileNotFoundError" in ret["p-alias-blocked"]["detail"],
          ret["p-alias-blocked"])
    check("别名退役：非别名策略不出条目（别把正常形态报成待办）",
          set(ret) == {"p-ok-alias", "p-alias-retirable", "p-alias-blocked"},
          sorted(ret))
    # 探针源码必须能编译（拼错的 f-string 只有真跑才炸）
    try:
        compile(PROBE_SRC, "<probe>", "exec")
        check("探针源码可编译", True)
    except SyntaxError as exc:  # pragma: no cover
        check("探针源码可编译", False, exc)
    return 0 if ok else 1


def _probe(context: str, namespace: str, image: str, configmap: str) -> dict:
    """在集群里跑一次性 pod 取事实（镜像档只有进镜像才看得见）。"""
    name = f"resprobe-{os.getpid()}"
    # canonical basename 也要量：别名策略只看得到自己那个 `_v2` 名字，
    # 基名脚本在各档的字节只有单独点出来才看得见（别名退役的唯一前置条件）。
    canon = ",".join(sorted({os.path.basename(p) for p in DEFAULT_MAP.values()}))
    overrides = json.dumps({"spec": {
        "imagePullSecrets": [{"name": "fd-harbor-pull"}],
        "containers": [{
            "name": "c", "image": image, "command": ["python", "-"], "stdin": True,
            "stdinOnce": True,
            "env": [{"name": "FD_OPEN_DATA_MCP_DATABASE_URL",
                     "valueFrom": {"secretKeyRef": {"name": "fd-db-secret",
                                                    "key": "DATABASE_URL"}}},
                    {"name": "FD_PROBE_NAMES", "value": canon}],
            "volumeMounts": [{"name": "fs", "mountPath": "/opt/fd-scripts",
                              "readOnly": True}],
        }],
        "volumes": [{"name": "fs", "configMap": {"name": configmap}}],
    }})
    proc = subprocess.run(
        ["kubectl", "--context", context, "-n", namespace, "run", name,
         "--rm", "-i", "--restart=Never", "--quiet", f"--image={image}",
         f"--overrides={overrides}"],
        input=PROBE_SRC, capture_output=True, text=True, timeout=300)
    for line in proc.stdout.splitlines():
        if line.startswith("PROBE_JSON "):
            return json.loads(line[len("PROBE_JSON "):])
    raise RuntimeError(f"探针没有输出 PROBE_JSON（rc={proc.returncode}）："
                       f"{(proc.stderr or proc.stdout).strip()[:400]}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--context", default="xinru")
    ap.add_argument("--namespace", default="fd-master")
    ap.add_argument("--configmap", default="fd-scripts")
    ap.add_argument("--image", default=None,
                    help="探针镜像（缺省取 crawl-reconciler 的镜像——策略脚本由它解析）")
    ap.add_argument("--probe-json", default=None,
                    help="用已有的探针产物（离线复核 / 回归），不去集群")
    ap.add_argument("--ref", default="origin/main")
    ap.add_argument("--repo", default=None, help="仓根（缺省=脚本所在仓）")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()

    repo = args.repo or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    if args.probe_json:
        try:
            with open(args.probe_json, encoding="utf-8") as f:
                facts = json.load(f)
        except Exception as exc:  # noqa: BLE001
            print(f"判不出（EXIT=2）：读不到探针产物 {args.probe_json}：{exc}",
                  file=sys.stderr)
            return 2
    else:
        image = args.image
        if not image:
            try:
                raw = subprocess.run(
                    ["kubectl", "--context", args.context, "-n", args.namespace,
                     "get", "cronjob", "crawl-reconciler", "-o", "jsonpath="
                     "{.spec.jobTemplate.spec.template.spec.containers[0].image}"],
                    capture_output=True, text=True, check=True).stdout.strip()
                image = raw
            except Exception as exc:  # noqa: BLE001
                print(f"判不出（EXIT=2）：取不到 reconciler 镜像：{exc}", file=sys.stderr)
                return 2
        try:
            facts = _probe(args.context, args.namespace, image, args.configmap)
        except Exception as exc:  # noqa: BLE001
            print(f"判不出（EXIT=2）：探针失败：{exc}", file=sys.stderr)
            return 2

    paths = sorted({p for p in DEFAULT_MAP.values()})
    repo_sha: dict[str, str | None] = {}
    for path in paths:
        try:
            blob = subprocess.run(["git", "-C", repo, "show", f"{args.ref}:{path}"],
                                  capture_output=True, check=True).stdout
            repo_sha[path] = hashlib.sha256(blob).hexdigest()
        except subprocess.CalledProcessError:
            repo_sha[path] = None

    results = resolve(facts["policies"], facts["files"], repo_sha, DEFAULT_MAP)
    enabled = [r for r in results if r["enabled"]]
    hits = [r for r in enabled if r["verdict"] in ("stale_effective", "missing")]
    und = [r for r in results if r["verdict"] == "undetermined"]

    print(f"direct 策略 {len(results)} 条（启用 {len(enabled)}）对 {args.ref}：")
    mark = {"ok": "✓", "stale_effective": "✗", "missing": "✗", "undetermined": "?"}
    for r in results:
        flag = "" if r["enabled"] else "（已停用）"
        print(f"{mark[r['verdict']]} {r['policy']}{flag} → {r['script']}: {r['detail']}")
        for inert in r.get("inert_lower_copy") or []:
            print(f"    ~ {inert['tier']} 里也有 {r['basename']}（{inert['bytes']} B）但"
                  f"字节不同——**改那一档不会生效**（镜像优先）")
    for a in alias_retirement(results, facts["files"], repo_sha, DEFAULT_MAP):
        if a["retirable"]:
            print(f"· 别名可退役：{a['policy']} 的 {a['alias']} → {a['canonical']}"
                  f"（{a['detail']}）")
        elif a["retirable"] is False:
            print(f"· 别名暂不可退役：{a['policy']} 的 {a['alias']} → {a['canonical']}"
                  f"（{a['detail']}）")
        else:
            print(f"· 别名退役判不出：{a['policy']} 的 {a['alias']}（{a['detail']}）")
    if hits:
        print("\n改法：① 让 effective 就是仓里那份——镜像档陈旧就重出该镜像；"
              "② 想立刻用仓里那份又不想出片，就**换名字**（`_v2` 别名）并在 "
              "`DEFAULT_MAP` 里写明真源。")
    if und:
        print(f"\n判不出 {len(und)} 条（映射缺 / 仓里没有对应文件）——缺数不是一致。")
        return 2
    return 1 if hits else 0


if __name__ == "__main__":
    raise SystemExit(main())
