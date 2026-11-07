# 任务清单

## 1. 规格

- [x] 1.1 `specs/crawl-job-lifecycle/spec.md` 写两个要求：Job 硬运行上限、超时 run 的收口语义
- [x] 1.2 `openspec validate add-crawl-job-deadline --strict` 通过

## 2. 实现（reconciler.py）

- [x] 2.1 新增模块级常量/函数 `_active_deadline_seconds()`：读 `SCRAW_JOB_ACTIVE_DEADLINE_SECONDS`，缺省 10800，非法值回退缺省并 warn
- [x] 2.2 `_scrapy_manifests()` 的 Job spec 加 `"activeDeadlineSeconds": _active_deadline_seconds()`
- [x] 2.3 `_direct_manifests()` 的 Job spec 同上
- [x] 2.4 `poll()` 识别 `DeadlineExceeded` 条件，收口 `policy_runs` 时写 `detail="deadline exceeded after <N>s"`，状态置 `failed`

## 3. 测试

- [x] 3.1 单测：两类 manifest 的 Job 均含 `activeDeadlineSeconds`，默认 10800
- [x] 3.2 单测：`SCRAW_JOB_ACTIVE_DEADLINE_SECONDS=120` 时 spec 取 120；非法值（`abc` / `-1`）回退 10800
- [x] 3.3 单测：`poll()` 对带 `Failed/DeadlineExceeded` 条件的 Job 返回终态并在 detail 标明
- [x] 3.4 集成（cheap 实跑）：`sleep 600` 探针 Job + `activeDeadlineSeconds=60` → 61s 转 `deadline_exceeded`（真 K8s 条件 `reason=DeadlineExceeded`，type=`FailureTarget`），`job_deadline_seconds()` 回读 60；探针已删、配额已回 12

## 4. 发布

- [ ] 4.1 构建镜像（sha tag）并经 TCR 发布
- [ ] 4.2 滚动 `fd-master` 的 `crawl-reconciler` 与 `coverage-expander`（先 suspend → 更新镜像 → resume）
- [ ] 4.3 上线后观察一轮 w208/w209 分片：无 DeadlineExceeded 误杀；卡死 Job 在 ≤3h 内自动收口

## 5. 收尾

- [ ] 5.1 台账 `paas/docs/registry-maintenance.md` 记一条（与 §15.138 续十二 呼应）
- [ ] 5.2 `openspec archive add-crawl-job-deadline`
