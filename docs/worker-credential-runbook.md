# worker 集群凭据：吊销与轮换手册

> 对应 ADR `docs/adr/0004-worker-cluster-credential-lifecycle.md` 与 change
> `rotate-worker-cluster-credentials`。本篇只写**怎么做**，取舍理由见 ADR。

## 0. 先认清三类状态

| 状态 | `/kubeconfigs` 里有 `<name>.token` | `clusters.enabled` | 该怎么办 |
|---|---|---|---|
| 启用 | 有 | `true` | 正常；定期轮换 |
| 禁用 | 无 | `false` | 正常；要启用时先签新凭据 |
| **残留** | 有 | `false` 或无该行 | **违规**，立即摘除 |

残留没有任何运行期症状：调度不会选禁用集群，日志也不会响。只能靠巡检发现：

```bash
python3 scripts/check_worker_credential_surface.py
# rc=0 一致 / rc=1 逐条点名 / rc=2 拿不到输入（不是"合规"）
```

## 1. 摘除残留凭据（安全，无停服风险）

```bash
# 看一眼现状：哪些集群有凭据
kubectl -n fd-master get secret worker-kubeconfigs -o json \
  | python3 -c "import json,sys;print(sorted(json.load(sys.stdin)['data']))"

# 摘掉某集群（token 与 ca 成对摘）
kubectl -n fd-master patch secret worker-kubeconfigs --type=json \
  -p '[{"op":"remove","path":"/data/baidu.token"},{"op":"remove","path":"/data/baidu.ca"}]'
```

摘除只改 Secret，不影响启用集群的派发。摘完必须跑 §4 的挂载点验证，
**不要用 `kubectl patch` 的输出来判断成功**（它会说 `patched` 甚至 `(no change)`
而值没变）。

## 2. 轮换一个启用集群的凭据

前提：`ClusterK8sClient` 每次构造都重读 `/kubeconfigs/<name>.token`，
CronJob 每次新建 pod，所以**改 Secret 即生效，不需要重启任何工作负载**。

### 2.1 legacy 形态（当前）

**注意：legacy token 不能单独吊销。** 下面这套只在「token 泄漏」时才有意义，
且必须连 ServiceAccount 一起换：

```bash
CTX=<worker 集群的 kubectl context>   # 例如 cheap-ts
# 1) 换身份（旧 token 立即全部失效 —— 派发会在这一刻断）
kubectl --context $CTX -n scraw delete sa crawl-worker
kubectl --context $CTX apply -f - <<'EOF'
apiVersion: v1
kind: ServiceAccount
metadata: {name: crawl-worker, namespace: scraw}
EOF
# 2) 让 token 控制器签发新值
kubectl --context $CTX apply -f - <<'EOF'
apiVersion: v1
kind: Secret
metadata:
  name: crawl-worker-token
  namespace: scraw
  annotations: {kubernetes.io/service-account.name: crawl-worker}
type: kubernetes.io/service-account-token
EOF
# 3) 取出新值（keys 带点，必须走 JSON 解析；jsonpath 会把 cheap.token 当嵌套路径）
kubectl --context $CTX -n scraw get secret crawl-worker-token -o json \
  | python3 -c "import json,sys,base64;print(base64.b64decode(json.load(sys.stdin)['data']['token']).decode(),end='')" \
  > /tmp/new.token
```

**代价要提前说明**：第 1 步生效的瞬间，所有用该 SA 的派发都会 401，直到第 4 步写完
Secret。没有回滚窗口（旧值已随 UID 变更失效）。因此这一步必须与「写回 + 验证」
放在同一次操作里做完。

```bash
# 4) 写回控制面（四个位置：fd-master 与 fd-mcp，各 cheap/tencent）
B=$(base64 < /tmp/new.token | tr -d '\n')
kubectl --context xinru -n fd-master patch secret worker-kubeconfigs --type=merge \
  -p "{\"data\":{\"<name>.token\":\"$B\"}}"
kubectl --context xinru -n fd-mcp  patch secret worker-kubeconfigs --type=merge \
  -p "{\"data\":{\"<name>.token\":\"$B\"}}"
```

### 2.2 bound 形态（未落地，待 ADR 裁决）

```bash
kubectl --context $CTX -n scraw create token crawl-worker --duration=720h
```

写回步骤同 2.1 的第 4 步。差别只有两点：值自带 `exp`；到期后需要重签。
若采纳此形态，必须先落地 change 任务 3.x（401 点名轮换命令 + 健康面暴露剩余寿命），
否则会引入「到期静默失效」——派发 401 而 Job 仍可能显示成功。

## 3. 恢复被摘除的集群

**不要从备份里取回旧 token**——它随 SA 重建已经失效（legacy）或已过期（bound）。
正确做法是给该集群签一个新凭据，然后：

```bash
# 1) 确认集群在表里且要启用
psql "$FD_OPEN_DATA_MCP_DATABASE_URL" -c \
  "select name, enabled from clusters where name='<name>'"
# 2) 签新凭据 → 写回两个命名空间（同 §2 第 4 步）
# 3) 把 enabled 置真（这属于业务决策，不是本手册自动做的）
```

## 4. 验收：只认挂载点实测

**判据必须成对，且必须发生在真实挂载点。**

```bash
kubectl --context xinru -n fd-master apply -f - <<'EOF'
apiVersion: v1
kind: Pod
metadata: {name: kcfg-probe, namespace: fd-master}
spec:
  restartPolicy: Never
  volumes: [{name: kubeconfigs, secret: {secretName: worker-kubeconfigs}}]
  containers:
    - name: probe
      image: ccr.ccs.tencentyun.com/finddata/fd-open-data-mcp:sha-9678e95
      volumeMounts: [{name: kubeconfigs, mountPath: /kubeconfigs}]
      command: ["python3","-c"]
      args:
        - |
          import json, os, ssl, urllib.request
          print("MOUNTED", sorted(os.listdir("/kubeconfigs")))
          token=open("/kubeconfigs/<name>.token").read().strip()
          ctx=ssl.create_default_context(cafile="/kubeconfigs/<name>.ca")
          req=urllib.request.Request("<api_server>/api/v1/namespaces/scraw/pods?limit=1",
              headers={"Authorization": f"Bearer {token}"})
          with urllib.request.urlopen(req, context=ctx, timeout=20) as r:
              print("RESULT OK", json.load(r).get("kind"))
EOF
kubectl --context xinru -n fd-master logs kcfg-probe
```

判定：

- 新值 → `RESULT OK PodList`
- 旧值 → **必须** 401（只验新值成功是不够的，旧值仍通等于没吊销）
- 摘除的集群 → `file_present=False`

**哪些不能当判据**（都是实测踩过的）：

| 反例 | 为什么不行 |
|---|---|
| Job `Complete` | `launched: []`（没活可派）时恒为真 |
| `kubectl patch` 输出 `patched` | 可能写的是与原来相同的值 |
| `kubectl patch` 输出 `(no change)` | 也可能是读法错误造成的假象 |
| `kubectl get secret -o jsonpath={.data.a.b}` | 点被当嵌套路径，恒为空串 |
| 本机 `kubectl --token=X auth can-i` | kubectl 会连 kubeconfig 的客户端证书一起送，量到的是证书权限 |

## 5. 到期前的眼睛：已经折进 scan，不再有独立 CronJob

2026-11-12 起，`crawl-watcher-scan`（每 15 分钟）自己就会检查凭据寿命：
`SCRAW_KUBECONFIGS_DIR=/kubeconfigs` + secret `worker-kubeconfigs` 挂载，
扫描输出的 JSON 里有 `credentials_checked` 与 `credential[]`。

**判据**：`credentials_checked: true` 才代表"查过了"。`false` 是「挂载没生效」，
不是「凭据都健康」——这两件事长得像，但一个是沉默的成功，一个是沉默的失效。

过渡期的 `CronJob/worker-credential-expiry` + `ConfigMap/credential-alert-scripts`
已于同日退役（`k8s/fd-master/60-credential-expiry-alert.yaml` 一并删除）。
退役顺序不能反：先换镜像并确认 `credentials_checked: true`，再删旧的；
反过来会有一段完全没有凭据到期告警的窗口。

要手工看一次当前寿命（只读）：

```bash
python3 scripts/check_worker_credential_surface.py --kubeconfigs-dir /kubeconfigs
```
