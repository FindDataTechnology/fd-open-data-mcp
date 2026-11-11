# ADR 0004: worker 集群凭据的形态与轮换

- **Status**: Proposed (2026-11-11；bound vs legacy 待裁决)
- **Context**: OpenSpec change `rotate-worker-cluster-credentials`
- **Deciders**: user（安全姿态与运维成本权衡）

## Context

控制面（`fd-master` / `fd-mcp`）向 worker 集群派活时，用每个 worker 的
`scraw/crawl-worker` ServiceAccount 的 token，存于 `worker-kubeconfigs`，
挂载为 `/kubeconfigs/<cluster>.{token,ca}`。`ClusterK8sClient` 每次构造都重读该文件，
因此这条通路是「Secret 即凭据」，更新 Secret 即生效，无需重启任何 pod。

2026-11-11 清理泄漏时实测出的三件事，决定了这个 ADR 必须存在：

**1. legacy token 无法单独吊销。** legacy SA token 是**确定性**签发的：同一 SA（同 UID）
+ 同 Secret 名 ⇒ 逐字节相同的 JWT。实测序列：

```
删掉 scraw/crawl-worker-token        → 旧 token 401   （看起来吊销成功）
按同名重建该 Secret                  → 新 token 与旧值完全相同（fp=195c0b07e7f2）
                                     → 同一个值又从 401 变回 200
```

即「删 Secret」只是让被引用的 Secret 不存在，属**暂停**；重建即复活。
真吊销只能删掉并重建 ServiceAccount（UID 变），代价是**同时打断全部派发**、
且旧 token 立即失效、没有回滚窗口。

**2. 这批 token 没有 `exp`。** 它们被明文提交进了 `finddata-openspec`
（`pre-teardown-secrets.yaml`），而该仓可被一个仍有效的 gitee 私人令牌读取。
凭据侧没有任何时间兜底：泄漏面持续存在，直到有人主动换 UID。

**3. 消费者跨集群、不可见。** 同一批 token 同时存在于
`xinru/fd-master/worker-kubeconfigs` 与 `xinru/fd-mcp/worker-kubeconfigs`；消费者是
`fd-master/coverage-expander`、`fd-master/crawl-reconciler`、`fd-mcp/fd-panel`。
吊销当天它们**没有报错**，因为 `clusters` 表里唯一 `enabled=t` 的 `cheap` 恰好
`launched: []`（没活可派）。**潜伏故障**，下一次有活才 401。

第 3 点附带一个判据教训：**Job `Complete` 在「没活可派」时恒为真**，不能作为
「凭据可用」的证据。凭据可用只能由「用真实挂载点发起一次读请求」证明。

## Decision

分两步，第一步已落地，第二步待裁决。

**已定（第一步）：凭据面收敛到「启用集群才有凭据」。**
`clusters.enabled=false` 或表里无行的集群，其凭据从控制面挂载 Secret 摘除；
由 `scripts/check_worker_credential_surface.py` 双向巡检
（禁用却在挂载里 / 启用却缺凭据 / 孤儿凭据），并在「挂载目录整体缺失」时返回
rc=2 而不是 rc=0 —— 不把挂载故障伪装成合规。

**待裁决（第二步）：凭据走 bound token 还是保持 legacy。**

| | bound token（`kubectl create token --duration`） | legacy（Secret 类 token） |
|---|---|---|
| 泄漏后能自愈吗 | 能，最多 N 天后自动失效 | 不能，永不过期 |
| 轮换成本 | 例行刷新（改 Secret 即生效，无需重启） | 只能换 SA UID，打断全部派发 |
| 新增故障模式 | **到期静默失效**：token 过期后派发 401，而 Job 可能仍显示成功（没活可派时） | 无（但也没有任何兜底） |
| 需要的配套 | 401 必须点名「凭据可能过期，运行轮换命令」；健康面暴露剩余寿命；刷新间隔 ≤ 寿命的 1/3 | 只需一个手写手册 |

倾向 bound，理由：它把「泄漏」从**永久事故**降级为**有界窗口**，而 legacy 的
「不会静默失效」这一优点，实际上已经被第 3 点抵消了——legacy 同样会静默失败
（只是失败原因是别人删了 Secret，而不是过期）。

但「到期静默失效」与今天踩的坑同型（`launched: []` 让 Job 恒绿），
所以配套的可见性是 bound 方案的**前置条件**，不是可选项。

## Consequences

- **不变**：派发协议、Job 形态、`/kubeconfigs` 挂载路径、RBAC
  （`ClusterRole/crawl-worker` 仅 `batch/jobs: create,get,list` +
  `crawl-worker-pod-reader` 的 pod 只读）。
- **变**：worker 凭据的写入从手工变成脚本；重新启用一个 worker 集群必须先签新凭据
  （这是刻意的：宁可显式失败，也不要复活旧值）。
- **恢复路径**：摘除前的 Secret 全量备份在
  `~/finddata/secrets/sa-token-backup/worker-kubeconfigs-{fd-master,fd-mcp}-20261111.yaml`；
  重新启用某集群时优先**新签**，而不是从备份取回（备份里的值已经随 SA 重建而失效）。
