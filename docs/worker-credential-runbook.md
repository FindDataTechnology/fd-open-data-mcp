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

**当前形态（2026-11-12 起）：`cheap9` 是 bound，720h。** 唯一启用的集群就是它；
`cheap` 已 `enabled=false` 且凭据已摘除（要重启它必须另签，见 §3）。

**没有自动化刷新**：`refresh-worker-credentials.yml` 需要 `WORKER_ADMIN_KUBECONFIGS`
与自托管 runner，两者都**刻意没有建**（把集群 admin kubeconfig 放进 GitHub secret
是一次不可逆的安全降级，只换来"自动化"）。所以下面这条是**唯一**的刷新路径，
reminder 也只有一条：`crawl-watcher-scan` 在剩余寿命 < 1/3（30d 寿命 ⇒ **10 天**）时
推一条微信。**剩 10 天还没人动，就是快到期了。**

```bash
# 一条命令完成「签 → 写两处 → 成对实测」（加法：旧值仍然可用，无断派发窗口）
python3 scripts/rotate_worker_credential.py --cluster cheap9 \
    --api-server https://100.64.0.17:6443 --mode bound --duration 720h --apply
# 只验当前值（只读）： 换 --verify-only
# 只打印计划（不动任何东西）：换 --dry-run
```

脚本的验收不是"返回 0"，而是成对实测：新值必须能打目标集群（PodList），旧值在
bound 模式下**仍应可用**（它签的是另一个凭据，worker 上的 legacy Secret 没动）。

### 2.1 legacy 形态（只在需要"当场吊销"时用）

**legacy token 不能单独吊销**，所以这套只在「token 泄漏」时才有意义，
且必须连 ServiceAccount 一起换：换 UID 的瞬间旧值全部失效——**派发会在这一刻断**，
没有回滚窗口，必须与"写回 + 验证"在同一次操作里做完。

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

```bash
# 4) 写回控制面（两个命名空间；当前只有 cheap9 有凭据）
B=$(base64 < /tmp/new.token | tr -d '\n')
kubectl --context xinru -n fd-master patch secret worker-kubeconfigs --type=merge \
  -p "{\"data\":{\"<name>.token\":\"$B\"}}"
kubectl --context xinru -n fd-mcp  patch secret worker-kubeconfigs --type=merge \
  -p "{\"data\":{\"<name>.token\":\"$B\"}}"
```

**注意 merge patch 的语义**：上面只写 `<name>.token` 这一个键。若你要一次改多个键，
必须把所有键一起给全——`data` 是整体替换的语义，漏掉的键会当场消失（历史上踩过）。

### 2.2 bound 形态（现用形态，含"为什么不再静默"）

```bash
kubectl --context $CTX -n scraw create token crawl-worker --duration=720h
```

与 legacy 的差别只有两点：值自带 `exp`；到期后需要重签。**这两点现在都不再静默**：
派发侧 401/403 会归因成 `WorkerCredentialError` 并在消息里带可执行的 rotate 命令，
scan 则在剩余 < 1/3 时告警。所以 bound 是默认选择——它的失效有提前量，legacy 的
失效没有（legacy 永不失效，也就不可能被提醒）。

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

下面的镜像 tag 只是示例——**照抄它会随发布变旧**，先取线上当前值：

```bash
kubectl --context xinru -n fd-master get cronjob crawl-watcher-scan \
  -o jsonpath='{.spec.jobTemplate.spec.template.spec.containers[0].image}'; echo
```

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
      image: ccr.ccs.tencentyun.com/finddata/fd-open-data-mcp:sha-9678e95   # 换成 cronjob 当前 tag
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

这条判据**2026-11-12 收紧过一次**：原先只要目录能读就算 `true`，于是"目录在、
但里面一个 `<cluster>.token` 都没有"（密钥键改名、Secret 被换空、路径挂错一级）
也报 `true` + `credential: []`，与"全都健康"逐字同形。现在 `true` 的含义是
**至少评过一个 token**；零个 token 会返回 `false` 并在日志里留一行
`里没有任何 <cluster>.token`。线上第一次见到 `false` 时先看那一行，别先怀疑凭据坏了。

过渡期的 `CronJob/worker-credential-expiry` + `ConfigMap/credential-alert-scripts`
已于同日退役（`k8s/fd-master/60-credential-expiry-alert.yaml` 一并删除）。
退役顺序不能反：先换镜像并确认 `credentials_checked: true`，再删旧的；
反过来会有一段完全没有凭据到期告警的窗口。

要手工看一次当前寿命（只读）：

```bash
python3 scripts/check_worker_credential_surface.py --kubeconfigs-dir /kubeconfigs
```

### 5.1 挂载面 vs `clusters` 表（2026-11-12 起也已自动查）

同一轮 scan 还会比对「挂载里有谁」与「表里谁 enabled」，折进**同一条**消息
（`credential_surface[]` / `credential_surface_checked`），按
`credential-surface:<cluster>:<kind>` 去重 24h。三种形态：

| kind | 含义 | 处置 |
|---|---|---|
| `orphan` | 挂载里有，表里没这个集群 | 删挂载（谁都不知道它属于谁） |
| `disabled` | `enabled=false` 却有凭据 | 删挂载（禁用集群留可用凭据＝后门） |
| `missing` | `enabled=true` 却没凭据 | 签发（否则派发在运行期才 401） |

判据仍是"真的查过吗"：目录不存在时 `credential_surface_checked=false` 且**静默**
（部署形态差异不是风险）。目录在但零 token 时 `true` + 一条 `missing`——空挂载
不再算绿。

**已知残留（刻意留的）**：整个 `/kubeconfigs` 挂载被移除时，两路检查都判 `false`
且都不告警。这是刻意的（"没配上不告警"），代价是"挂载被误删"这件事没有人喊——
它由 `k8s/fd-master/30-crawl-watcher-scan.yaml` 的 volume 声明保证，而删掉挂载本身
会立刻打断派发，属"坏得很响"的那一类。
