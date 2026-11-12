## 1. 回合核对（reconciler）

- [x] 1.1 `MultiClusterLauncher.launch()` 在建 Job 抛异常后按固定名字回读一次，存在即采纳——验证：单测里让 `create` 抛 `TimeoutError` 且回读拿到该 Job，`launch()` 不抛且返回正确 `job_ref`
  实现取 `ClusterK8sClient.get_job()`（新方法，返回原始 Job 对象；`job_status` 只回状态枚举，
  拿不到 `creationTimestamp`，且它会吞错）。`tests/test_launch_lost_response.py::test_launch_does_not_resend_the_create_after_a_lost_response`。
- [x] 1.2 不存在时保留原异常路径，并在异常信息里标明"已回合核对：Job 不存在"——验证：回读抛 404 时异常照旧抛出且文本含核对结论
  `LaunchResponseLost` 的文案是 `… — 已回合核对：Job <name> 不存在（干净失败，可安全重试）`，调用方
  `_launch_direct` 仍落 `failed`（`test_launch_direct_records_failed_when_absence_is_confirmed`）。
- [x] 1.3 采纳时打一条 warning，含 cluster / job 名 / 原异常文本 / `creationTimestamp`——验证：caplog 抓到该条且四个要素齐全
  `test_adopts_a_job_that_landed_before_the_timeout` 断言四个要素逐个出现。这是"响应丢失"唯一留下的信号，
  否则它会静默变成一次普通成功。
- [x] 1.4 采纳窗口之外的对象不认（防采纳同名的历史 Job）——验证：`creationTimestamp` 早于窗口时仍按失败处理
  窗口 `LOST_RESPONSE_ADOPTION_WINDOW = 15min`（名字带秒级时间戳，真匹配都在秒级，窗口只为挡住病态同名）。
  `test_stale_same_named_job_is_not_adopted`、`test_missing_creation_timestamp_is_not_adopted`。
- [x] 1.5 不重发创建请求——验证：`create` 调用计数在异常路径上恰为 1
  同一测试断言 `seen == {"create": 2, "get": 1}`（ConfigMap + Job 各一次，之后只回读一次）。

**多出来的一条（计划里没写，但绕不过去）：回读本身失败时不许声称"不存在"**——
三种结局必须分开：存在（采纳）/ 404（确认缺失）/ 读失败（无法确认）。读失败仍走失败路径，
但文案写"无法确认是否落地"。理由：把"读不到"当"不存在"，会让一个真在跑的 Job 被记成 failed（重复抓取）；
反过来把"无法确认"当"在跑"，行会永远挂着并**永久堵住该策略的 single-flight**（`poll()` 对
`unknown` 不关行），后者更坏。`test_unreadable_cluster_does_not_claim_absence` 专门守这条，
反向验证时把它改回"不存在"文案 → 该测试真红。

## 2. 回归

- [x] 2.1 正常路径不变：`create` 成功时 `launch()` 返回相同的 `job_ref`，无新增日志噪声——验证：既有 launcher 单测全绿
  `tests/test_launch_lost_response.py tests/test_reconciler.py tests/test_multi_cluster.py tests/test_direct_executor.py`
  → **59 passed**。新增日志只在异常分支里打，正常路径只多一层 `try`。
- [x] 2.2 `_launch_direct` / `launch_policy` 的记账不变：成功行 `status=running` 且 `job_ref`/`cluster_id` 齐全；失败行仍 `failed`——验证：两条路径各一个单测
  成功侧沿用既有 `test_direct_policy_launches_and_tracks_run`（`running` + `job_ref=aliyun/…` + `cluster_id=3`）
  与 `test_reconciler.py::test_reconciler_launches…`；失败侧新增上面那条 `_launch_direct` 记账测试。
- [x] 2.3 `openspec validate reconcile-lost-launch-response --strict` 通过
  `Change 'reconcile-lost-launch-response' is valid`（首次报 INFO：我把新增需求写成 `MODIFIED`，
  改为 `ADDED` 后干净）。

## 3. 真实集群上的验证（不是打桩）

- [x] 3.1 用真实 `ClusterK8sClient` 对 cheap 打两个分支
  建一个 echo Job（真实 `creationTimestamp`），再拿它调 `adopt_job_if_present(…, TimeoutError("The read operation timed out"))`
  → 采纳、未抛异常，warning 逐字为
  `launch response lost but job exists; adopting cluster=cheap job=lost-probe-1794382621 created=2026-11-11T07:37:01+00:00 err=TimeoutError: The read operation timed out`；
  拿一个不存在的名字调 → `LaunchResponseLost: timed out — 已回合核对：Job … 不存在（干净失败，可安全重试）`。
  真 `creationTimestamp` 形如 `2026-11-11T07:37:01Z`，解析正确。探针建的 Job 已删。
  这一步同时证明新增的 `get_job()` 在真实 API 上可用（严格校验 `cheap.ca`，不复用 kubeconfig 的 insecure 姿态）。

## 4. 收口

- [x] 4.1 归档——验证：`openspec archive reconcile-lost-launch-response --yes --strict` 成功
  **2026-11-12 归档**（用户裁决：没有真实客户、系统一直在动，不等观察周期）。
- [x] 4.2 判据改为构造测试：`policy_runs` 不再出现"detail 含 read timed out 但同名 Job 已 succeeded"的行
  （判据必须是**对照两侧**：既看 detail，也看 worker 上有没有同名 Job；只看日志会漏）
  **为什么不看线上了**：这一条原本要求部署后观察一个周期；用户 2026-11-12 裁决用构造断言替代
  （等一周的价值不如一条能红的断言）。落在 `tests/test_launch_lost_response.py`：
  - `test_adopted_launch_leaves_no_timeout_in_the_ledger`：adopted → 行保持 `running`、`job_ref`
    与 `cluster_id` 都在、`detail` 为空（不含 timed out）；
  - 邻居 `test_launch_direct_records_failed_when_absence_is_confirmed`：确认不存在 → `failed` +
    `不存在` 文案 + `job_ref is None`。
  两条正好构成任务要求的**对照两侧**。注入验证：把 adopted 写成 failed 并写 timeout 文案 → 前者红；
  还原后全绿。函数级判据（adopt / 404 / 读失败 / 过期 Job / 缺时间戳）另有 8 条既有用例。
- [ ] 4.3 上线（**仍未做**）
  线上 reconciler 是 `scraw-fd-open-data-mcp:sha-daec768` = `fd_open_data_mcp 0.5.61`（pod 内实测），
  本修复不在其中。PyPI 0.5.62 已发（2026-11-12），剩下的卡点是**镜像**：`crawl-reconciler` 不能换
  成本仓镜像——实测 `fd-open-data-mcp:sha-9678e95` 里没有 `/app/scripts`、没有 `bulk_ingest_*.py`，
  而 `_read_script()` 要从镜像里取它们拼 ConfigMap，换过去会打断派发。
  链路：重建 scraw 镜像（`FD_ODM_INSTALL` 下限抬到 0.5.62）→ 改 `crawl-reconciler` 的 image。
  上线判据只认镜像内实测：`python -c "import fd_open_data_mcp; print(__version__)"` 为 0.5.62，
  且 `grep -c adopt_job_if_present` 非零；不看 tag 名、不看 CI 绿灯。
