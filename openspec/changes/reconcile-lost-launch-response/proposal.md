## Why

派发一条 crawl Job 时，控制面向 worker 集群发的 `POST /apis/batch/v1/.../jobs`
**已经把对象建出来了**，但客户端在等响应体时读到 30s 超时，于是整条 launch 抛异常，
`policy_runs` 落成 `failed` 且 `job_ref` / `cluster_id` 都是空。Job 随后自己跑完了，
数据也落了库——账本上却是一次失败，而且那个 Job 没有任何行指向它。

2026-11-11 在 cheap（唯一 `enabled=t`）上实测到两例，同一个 tick：

- `policy_runs` 2347 / policy 2487：`status=failed`、`cluster_id=NULL`、`job_ref=NULL`、
  `detail='launch failed: The read operation timed out'`，起于 `06:37:04Z`。
- 同名 Job `crawl-policy-2487-20261111063707` 在 cheap 上 `succeeded=1`。
  名字里的 `063707` 是**构造清单**的时刻，`startTime` 是 `06:37:54Z`——比它晚 47 秒，
  正好落在读超时窗口之后。即：Job 在建单后 3 秒就成了，只是响应没读回来。

同一轮里 policy 2 的失败是 `detail='launch failed: <urlopen error _ssl.c:994:
the handshake operation timed out>'`——**握手就没成，确实什么都没执行**。
这两种失败在账本上完全同形，但语义相反：一个该重试，一个重试就会重复抓取。

后果是三重的：

1. **重复执行。** 下个周期同窗口重新派发，同一段数据被抓两遍（upsert 能掩盖一部分，
   但"本周期已完成"的判断是错的）。
2. **不可见、不可取消。** 那个 Job 没有 `job_ref`，面板上看不到，`cancel` 也不会去删它。
3. **错误的运营读数。** 失败率、成功率都被这种"假失败"污染。

这不是凭据问题（新 token 建单成功、同一 tick 里另外两条策略正常启动），
而是**"响应丢失"被当成了"请求未执行"**。

## What Changes

- **launch 的异常分支要先回合核对再判定失败。** `MultiClusterLauncher.launch()` 在
  建 Job 抛异常后，SHALL 按**发送前就已确定的名字** `GET` 一次该 Job：
  - 存在且 `creationTimestamp` 在合理窗口内 ⇒ 视为**已派发**，正常返回
    `(job_ref, cluster_id)`，并记一条 warning 说明"响应丢失但对象已存在"。
  - 不存在（404）⇒ 保留原来的异常路径（仍记 `failed`），但 detail 里写明
    "已回合核对：Job 不存在"，把"干净失败"和"不确定"分开。
- **名字必须在发请求之前就定下来并可用于复核。** 现在 `name` 在 `launch()` 内部构造，
  异常抛出后调用方拿不到它。改动后 `launch()` 自己完成核对，调用方的契约收紧为：
  *要么抛异常且确认没有对象落地，要么正常返回 job_ref*。
- **不改超时值、不改重试策略。** 30s 读超时本身是合理的；问题在于失败归因，不是等待时间。
  自动重试 POST 会更糟（可能建出第二个 Job）。

## Capabilities

### New Capabilities
（无）

### Modified Capabilities
- `crawl-job-lifecycle`: 新增一条要求——launch 失败判定 SHALL 以"对象是否真的不存在"
  为依据，而不是以"是否抛了异常"为依据；`policy_runs` 不应出现"记 failed 但 Job 实际
  跑完"的行。

## Impact

- **`fd_open_data_mcp/refresh/reconciler.py`**：`MultiClusterLauncher.launch()` 增加
  异常后的回合核对；抽一个可复用的 `_adopt_job_if_present(client, name, started_at)`
  小函数（纯逻辑，便于单测）。`_launch_direct` / `launch_policy` 的记账逻辑不变。
- **DB**：无 schema 变更。既有的"假失败"行**不回填**（它们的 Job 早已被 TTL 收掉，
  现在回填只会制造新的假数据）；只在文档里记录这次取证。
- **可观测性**：新增一条 warning 日志（`launch response lost but job exists; adopting`），
  带 cluster、job 名、异常文本、adopted `creationTimestamp`。这是唯一能看出"LOST_RESPONSE"
  发生过的信号——否则它会静默变成一次普通成功。
- **不涉及**：单集群 `K8sJobLauncher`（走 `kubectl create`，超时语义不同）本次不改，
  但同一套判据适用于它，留作后续。
