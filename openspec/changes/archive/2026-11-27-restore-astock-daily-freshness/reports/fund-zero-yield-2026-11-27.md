# 基金五条 zero_yield 的定位（1.2，2026-11-27）

结论先行：**不是上游封禁，是旧镜像里的一条写侧缺口**；当前镜像（`sha-a6a9240`）跑同一份
策略计划**成功写入 3588 行、exit 0**。所以基金零产出与 2659 同源（执行镜像太旧），
随本次 5.2b 一起滚已修。

## 证据一：上游给了数据（排除「上游拒绝」）

`fetch_log`（canonical PG）对 policy 3（fund-nav-daily，concepts 378/379）在 **2026-11-20**：

| status | 条数 | detail |
|---|---|---|
| ok | 204 | 空（HTTP 200） |
| error | 6 | `akshare fund_open_fund_info_em failed ... SyntaxError: Unexpected token '<'`（偶发 JS 壳） |

即：那一天**真的发了请求且 204 次成功**，底层函数 `fund_open_fund_info_em` 每次拿回
20,798 行 frame。**上游不是拒绝方**——如果是「上游全拒」形态，fetch_log 会是成片 error。

## 证据二：旧窗口里数据没落地（写侧缺口）

`semantic_observations_all` 对 concepts 378/379 的日计数：

```
2026-11-19 → 104
2026-11-20..26 → 0   （整整一周零落地，而 fetch_log 11-20 有 204 次 ok）
2026-11-27 → 598     （下面是本次调试跑写入的）
```

「取到了、没写进去」——正是 `zero_yield`（`rows_attempted=0/rows_new=0`）的形态，
且 `policy_runs` 里 `rows_attempted` 一直是 `None`（pod 在首次上报前就结束）。
两者叠加：旧执行体取到 frame 后**在写库前就退出**（或没走到 pipeline），于是既没落地、
也没上报。

## 证据三：当前镜像跑同一计划成功

按 `docs/astock-daily-increment.md` 的「派生调试 Job、保留 pod」配方，对一个**真实的
12 格计划**（policy 3 的 planner 输出，concepts 378/379、6+6 个日期、函数
`fund_open_fund_info_em`）在 cheap9 派生一条不删 pod 的 Job（backoffLimit=0、去 TTL、
restartPolicy=Never、activeDeadlineSeconds=3600），镜像用**当前** `clusters.image`
`scraw-fd-open-data-mcp:sha-a6a9240`：

```
scrapy start_requests concepts=2 mode=per_date
  concept=nav.unit ... -> 6 dates ; concept=nav.accumulated ... -> 6 dates
downloader/response_status_count/200: 12
snapshot akshare/fund_open_fund_info_em: 20798 frame rows -> 299 in-scope items  (×12)
retention: T 合并 3588 行 / W 淘汰 0 行
item_scraped_count: 3588     finish_reason: finished     elapsed: 109s
containerState.terminated.exitCode = 0  reason = Completed
```

落地核对：concepts 378/379 的 `semantic_observations_all` 11-27 新增 598 行。

## 为什么旧镜像失败、新镜像成功

- akshare/pandas 的 pin 在两个镜像间**完全相同**（`akshare>=1.18.94`、`pandas>=3.0.5`），
  所以不是依赖漂移。
- 唯一变量是 fd 代码：旧 `FD_ODM_REF=82831c9`（2026-11-13）→ 新 `4377a8f`（100 个提交）。
  写侧（pipeline / upsert / 列映射）的差异落在这一段。旧镜像 `sha-73e7c54` 已从 ccr
  轮转删除，无法在线 A/B；但「同计划、同 akshare、同源函数，旧镜像 0 行、新镜像 3588 行」
  已足以定性。

## 处置与遗留

- **已修（随 5.2b 同一滚动）**：`clusters.image`(cheap9)、reconciler、policy-freshness、
  SCRAW_K8S_IMAGE 全部 → `sha-a6a9240`。今晚 06:37 CST 起的定时 scrapy 轮会走新镜像。
- **待观察（与 5.2b 同一条观察窗）**：定时轮经**完整 launcher**（带 `SCRAW_JOB_REF`）
  跑完后，`policy_runs` 应出现 `rows_attempted>0 && rows_new>0`（不再是 zero_yield）、
  且 `semantic_observations_all` 对应日期有行。本次调试跑绕过了 launcher（无
  `SCRAW_JOB_REF`），只证「抓取+落地」，未证「上报」。若定时轮仍 zero_yield 但数据落地，
  则是上报通道问题（另查 `report_run_yield`/`SCRAW_JOB_REF`），不是抓取。
