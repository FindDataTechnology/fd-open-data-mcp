# 给爬虫 Job 加硬运行上限（activeDeadlineSeconds）

## Why

2026-11-07 的事故：fd-deploy（3.7GB）上 16 个 `covexp-w208-p1..p16` 一次性 Job 在 swap thrash 期间**卡在 `Running 0/1` 达 4h48m**，各吃 ~90MB，把宿主压到 available 112MB，连带 tailnet 全断、`auth.finddatatech.cloud` 全站登录不可用。

现有防护不生效：三个 Job spec 都已经有 `ttlSecondsAfterFinished: 86400`，但 **TTL 只在 Job 进入完成态后计时**——Running 卡死的 Job 永远不触发，于是无限堆积，只能靠人工 `kubectl delete job` 清理（还要顺手把 `policy_runs.status='running'` 的行收口，否则 coverage-expander 读到 `open_runs=16` 而拒绝规划下一波）。

## What Changes

1. 给 `MultiClusterLauncher` 生成的三类 Job（scrapy / direct / station 之外的 coverage 分片）加 **`activeDeadlineSeconds`**，给"卡死"一个硬上限。
2. 上限可由环境变量 `SCRAW_JOB_ACTIVE_DEADLINE_SECONDS` 覆盖（默认值取当前最长正常批次的 2 倍以上，避免误杀长任务）。
3. Job 因 deadline 被终止时，`poll()` 收口 `policy_runs` 时在 `detail` 里写明 `deadline exceeded`，便于区分"源站慢"与"进程卡死"。
4. 该行为只在新建 Job 时生效——存量 Job 不动（避免对在跑批次突然变更）。

## Impact

- Affected specs: `crawl-job-lifecycle`（新增能力）
- Affected code: `fd_open_data_mcp/refresh/reconciler.py`（`_scrapy_manifests` / `_direct_manifests` 的 Job spec、`poll()` 的收口文案）
- 生效条件：需重建控制面镜像并滚动 `crawl-reconciler` / `coverage-expander` 两个 CronJob
- 风险：上限设太小会杀掉正常长批次（`estimate≈45k` 的分片属正常慢任务）→ 默认值须保守，且可 env 覆盖
