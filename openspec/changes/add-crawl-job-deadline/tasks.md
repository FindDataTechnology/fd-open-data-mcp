# 任务清单

## 1. 规格

- [x] 1.1 `specs/crawl-job-lifecycle/spec.md` 写两个要求：Job 硬运行上限、超时 run 的收口语义
- [x] 1.2 `openspec validate add-crawl-job-deadline --strict` 通过

## 2. 实现（reconciler.py）

- [x] 2.1 新增模块级常量/函数 `_active_deadline_seconds()`：读 `SCRAW_JOB_ACTIVE_DEADLINE_SECONDS`，缺省 10800，非法值回退缺省并 warn
- [x] 2.2 `_scrapy_manifests()` 的 Job spec 加 `"activeDeadlineSeconds": _active_deadline_seconds()`
- [x] 2.3 `_direct_manifests()` 的 Job spec 同上
- [x] 2.4 `poll()` 识别 `DeadlineExceeded` 条件，收口 `policy_runs` 时写 `detail="deadline exceeded after <N>s"`，状态置 `failed`
- [x] 2.5 三条传输共用一个 `_job_state(status)` 解析器（in-cluster REST / 跨集群 REST / 本地 kubectl）；kubectl 分支改取 `-o json`，废弃按位置读 jsonpath token
- [x] 2.6 `_failed_condition()`：`Failed=True` 条件本身即终态（0.5.42，无 `status.failed` 计数的饥饿 Job）

## 3. 测试

- [x] 3.1 单测：两类 manifest 的 Job 均含 `activeDeadlineSeconds`，默认 10800
- [x] 3.2 单测：`SCRAW_JOB_ACTIVE_DEADLINE_SECONDS=120` 时 spec 取 120；非法值（`abc` / `-1`）回退 10800
- [x] 3.3 单测：`poll()` 对带 `Failed/DeadlineExceeded` 条件的 Job 返回终态并在 detail 标明
- [x] 3.4 集成（cheap 实跑）：`sleep 600` 探针 Job + `activeDeadlineSeconds=60` → 61s 转 `deadline_exceeded`（真 K8s 条件 `reason=DeadlineExceeded`，type=`FailureTarget`），`job_deadline_seconds()` 回读 60；探针已删、配额已回 12
- [x] 3.5 回归单测：饥饿形状（`active/succeeded/failed` 全 null，只有 `FailureTarget`+`Failed` 条件）在 REST 与 kubectl 两条分支都必须判 `deadline_exceeded`。现场证据：`zz-starve-probe`（`requests.cpu=20` 永远排不上）45s 后被 K8s 终止，`kubectl` 输出 `active=None succeeded=None failed=None` + 两条件；旧 jsonpath 位置解析把它读成 `success`（假成功）
- [x] 3.6 `pytest tests/test_reconciler.py -q` → 36 passed

## 4. 发布

- [x] 4.1 构建镜像（sha tag）并经 TCR 发布：v0.5.41 → `sha-5c91878`，v0.5.42 → `sha-76f3e23`（hkccr 推送，cheap3 cron `tcr-relay-sync.sh` 回灌 ccr）
- [x] 4.2 滚动 `fd-master` 的 `crawl-reconciler` 与 `coverage-expander`（先 suspend → 更新镜像 → resume）；两个 CronJob 与 `clusters.image` 均已是 `sha-76f3e23`，generation 30 / 20，19:22Z 的 tick 在新镜像上正常收口（`launched: []` 无误判）
- [x] 4.3 观察：清理那批卡死 Job 时 12 条 run 经新代码路径收口（`detail=deadline exceeded after 60s`，无一条健康 Job 被误杀）；新派发真带 `activeDeadlineSeconds=10800`（`crawl-policy-2659-20261107192140`，实读 Job spec 确认）；`astock-daily-increment` 在 0.5.42 镜像上派发成功（此前 18:10 的 `/kubeconfigs/cheap.token` ENOENT 未复现）
- [x] 4.4 v0.5.43（3.5 的 kubectl 假成功修复）出片并回滚两 CronJob：`sha-f72bf16`，随后 0.5.44 `sha-7e97115` 再次滚动；两 CronJob（`suspend=false`、image jsonpath 复核）与 `clusters.image` 均已对齐，`scraw-fd-open-data-mcp/k8s/*-cronjob.yaml` 同步以免下次 apply 回退

## 5. 收尾

- [x] 5.1 台账 `paas/docs/registry-maintenance.md` §15.138 **续十四** 记全链（0.5.42 无 pod 的 deadline 条件形状、0.5.43 kubectl 假成功机理、0.5.44/0.5.45 census 三缺陷、PyPI simple-index 滞后、`kubectl apply` 不换运行中容器的镜像、DiskPressure 拦截新 pod）
- [ ] 5.2 `openspec archive add-crawl-job-deadline`
