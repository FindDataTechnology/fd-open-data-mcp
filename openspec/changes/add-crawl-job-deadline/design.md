# 设计：Job 硬运行上限

## 现状证据（2026-11-08 实测，policy_runs 近 30 天）

| status | 次数 | 平均时长 | 最长 |
|---|---|---|---|
| success | 141 | 23 min | **159 min** |
| zero_yield | 21 | 17 min | 49 min |
| no_op | 90 | 15 min | 15 min |
| redundant | 128 | 8 min | 15 min |
| cancelled | 16 | 322 min | 322 min（本次人工收口的卡死批次） |

结论：正常批次天花板 ≈ **160 分钟**；卡死批次达到 322 分钟且没有自然终点。

## 决策

1. **默认 `activeDeadlineSeconds = 10800`（3h）**：= 正常最长 159min 的 1.9 倍余量，同时明显低于"无终点"的卡死态。可用 `SCRAW_JOB_ACTIVE_DEADLINE_SECONDS` 覆盖（运维对超长回补批次临时放宽）。
2. **只加在 scrapy / direct 两类业务 Job**上；station 类已在 `station_ops.py:529` 有自己的窗口 deadline，不重复设置。
3. **`ttlSecondsAfterFinished: 86400` 保留**：deadline 负责把卡死的 Running 变成 Failed，TTL 负责随后回收对象——两者串联才闭环（只有 TTL 时卡死 Job 永不回收，这正是本次事故）。
4. **收口文案**：`poll()` 看到 Job 的 `status.conditions` 含 `type=Failed, reason=DeadlineExceeded` 时，写 `policy_runs.detail = "deadline exceeded after <N>s"`，让"源站慢"与"进程卡死"在台账上可区分。
5. **不改存量 Job**：只影响新建 Job，避免对在跑批次突然变更运行语义。

## 为什么不选其它方案

- **提高 `backoffLimit`**：卡死是"容器还活着但不推进"，不触发重启，无效。
- **加 livenessProbe**：爬虫无统一健康端点，且在换代理/长翻页时会假死，误杀率高。
- **只加集群侧 janitor CronJob 删老 Job**：能清对象，但会把还在正常跑的批次一起杀掉，且 `policy_runs` 仍留 running 行（本次已踩：扩容器被 `open_runs=16` 闸住）。
- **降 `clusters.capacity`**：只是限并发，卡死 Job 依旧永久占坑，治标。

## 验证

- 单测：构造 Job spec 断言含 `activeDeadlineSeconds` 且能被 env 覆盖。
- 集成：在 cheap 上跑一个 `activeDeadlineSeconds=60` 的探针 Job（命令 `sleep 600`），确认 60s 后 Job 变 Failed、`detail` 含 `deadline exceeded`，随后被 TTL 回收。
- 回归：正常批次（estimate≈45k）不被 3h 上限影响——抽查一轮 w208 分片时长。
