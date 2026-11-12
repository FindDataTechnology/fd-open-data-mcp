## Why

控制面（`fd-master` / `fd-mcp`）向 worker 集群派活时，用的是每个 worker 的
`scraw/crawl-worker` 这个 ServiceAccount 的 **legacy token**，存于
`worker-kubeconfigs` Secret，挂载为 `/kubeconfigs/<cluster>.{token,ca}`。
`ClusterK8sClient._load()` 每次构造都重读该文件，因此这条通路是"Secret 即凭据"。

2026-11-11 排障时暴露出三个成体系的问题，单看任何一个都不像故障：

**1. 这类 token 无法单独吊销。** legacy SA token 是**确定性**签发的——同一 SA（同 UID）
+ 同 Secret 名 ⇒ 逐字节相同的 JWT。实测：删掉 Secret 后旧 token 立刻 401（看似吊销成功），
但按同名重建 Secret 后，新 token 与泄漏值**完全相同**，同一个值又从 401 变回 200。
真吊销只能删掉并重建 ServiceAccount 换 UID，而这一步会**同时打断所有在用该 SA 的派发**。

**2. 它们没有有效期，因此泄漏永不过期。** 这批 token 被明文提交进了
`finddata-openspec` 仓（`pre-teardown-secrets.yaml`），而该仓可被一个仍然有效的
gitee 私人令牌读取。也就是说：泄漏面持续存在，且凭据侧没有任何时间兜底。

**3. 删除凭据前，消费者是跨集群的、不可见的。** 同一批 token 同时存在于
`xinru/fd-master/worker-kubeconfigs` 与 `xinru/fd-mcp/worker-kubeconfigs`；
消费者是 `fd-master/coverage-expander`、`fd-master/crawl-reconciler`、`fd-mcp/fd-panel`
（都挂 `/kubeconfigs`）。吊销当天它们**没有报错**——因为那几轮 `clusters` 表里
唯一 `enabled=t` 的 `cheap` 恰好 `launched: []`（没活可派）。**潜伏故障**，
下一次有活才 401。

第 3 点尤其危险：现有判据（Job 是否 `Complete`）在"没活可派"时永远为真。

## What Changes

- **给 worker 凭据定一个可轮换的形态**：不再依赖"Secret 永不过期"这一不可撤销机制。
  取 `kubectl create token` 的 bound token（带 `exp`），由控制面侧定期刷新写回 Secret；
  `_load()` 每次新建客户端、CronJob 每次新建 pod，因此**更新 Secret 即生效，无需重启**。
- **把轮换动作脚本化**：一条命令完成"给某 worker 签新 token → 写回
  `worker-kubeconfigs` → 用挂载点实测一次 `PodList`"，避免手工 `base64` 拼接
  （手工路径已经踩过 `jsonpath` 把 `cheap.token` 当嵌套路径、以及 patch 报
  `no change` 却静默无效两个坑）。
- **停用集群的凭据从挂载 Secret 里摘掉**：`clusters.enabled=f` 的集群（当前
  `tencent` / `baidu` / `america`）不应继续把可用凭据挂在控制面 pod 上。
  需要重新启用时再签新 token。
- **把"凭据吊销"的验收判据固定为成对验证**：旧值必须失败、新值必须成功，
  且必须在**真实挂载点**验证（起一个挂同一 Secret 的探针 pod 去打 API），
  不以 `kubectl patch` 的输出、也不以 Job `Complete` 作为判据。

**BREAKING**：无。属于凭据生命周期与运维脚本的收敛，不改变派发协议、
不改变 `clusters` 表结构、不改变 `/kubeconfigs` 的挂载路径。

## Capabilities

### New Capabilities

（无——沿用既有的 `source-credential-hygiene`，它的 Purpose 已经写死
"凭据经环境/Secret 进入进程，绝不经过命令行、pod spec 字段或日志行"，
本变更补的是**生命周期**这一维。）

### Modified Capabilities

- `source-credential-hygiene`: 现有唯一需求只约束"DSN 不得出现在命令行"。
  本变更新增两条需求——worker 集群凭据必须**可轮换且有界寿命**，
  以及**禁用集群的凭据必须从挂载 Secret 中移除**——把该 spec 从
  "凭据怎么进进程"扩展到"凭据怎么退出"。

## Impact

- **代码**：`fd_open_data_mcp/refresh/reconciler.py` 的 `ClusterK8sClient._load()`
  （读凭据的入口，本次不改其逻辑，只是把"token 会过期"写进契约）；
  新增一个轮换脚本（放 `scripts/`）。
- **运维**：`worker-kubeconfigs` Secret 的写入方式由手工变为脚本；
  增加一个刷新节奏（bound token 的 `exp` 决定频率）。
- **集群**：`scraw/crawl-worker` 的 SA 不再需要"永不过期 Secret"这条兜底；
  禁用集群的 token 从控制面 Secret 移除后，重新启用需签新 token。
- **不变**：派发协议、Job 形态、`/kubeconfigs` 挂载路径、RBAC
  （`ClusterRole/crawl-worker` 只给 `batch/jobs: create,get,list` +
  `crawl-worker-pod-reader` 的 pod 只读，本次不动）。

## 为什么不是"什么都不做"

保持现状的代价已经被实测过一次：凭据泄漏进 git 之后，**唯一**的处置手段是
"删 SA 重建"——而那条路会同时打断全部派发，且没有回滚窗口（旧 token 立即失效）。
有界寿命 + 脚本化轮换把这件事从"故障处置"降级为"例行刷新"。
