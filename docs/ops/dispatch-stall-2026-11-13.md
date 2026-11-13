# 派发停更事故：一条悬着的 running 行让策略永久死掉（2026-11-13）

> 本页只解决三件事：**症状长什么样、根因在哪一行、修了什么、周一该看什么。**
> 数字全部来自 `fd-prod` 实查（2026-11-13），每条都能用文末 SQL 复现。

## 症状：不是宕机，是"什么都没发生"

- `policy_runs` 里 policy 2659（`astock-daily-increment`，cron `17 18 * * 1-5`）有一条
  **`running` 挂了 5.2 小时**的行：`id=2383`，`started_at=2026-11-13 10:22:04`，`finished_at=NULL`，
  `detail=''`。
- 当天 **18:17 CST 的计划内执行没有留下任何行**——`policy_runs` 中午之后 0 条。
- 上游数据停在 **2026-09-18**（`price.close`/`stock`），而基金同日到 **2026-11-13**：
  同一个抓取器、同一个库、同一段时间，坏的只有这一条。
- 所有"服务健康吗"的判据都是绿的：`kubectl get pods` 正常、panel 只显示"最近一条是 running"、
  没有告警。

## 根因：`unknown` 永远不关行

`reconciler.poll()` 在 Job 读不到时（`activeDeadlineSeconds` 杀掉 → `ttlSecondsAfterFinished`
回收 → API 404）捕获异常并 **`return "unknown"`**：

```python
except Exception:            # add-crawl-job-deadline: api unreachable / job gone
    return "unknown"
```

而 `reconcile_once` 的完成探针只在 `{success, failed, deadline_exceeded}` 时关行。
**`"unknown"` 不在这个集合里，于是那条行永远不进入终态。**

派发是单飞的（存在 open run 就跳过），所以：**一条悬着的行 = 这条策略永久停更**。
而"被跳过"当时只写日志；执行器 pod 已被 `ttl` 回收，日志也抓不到
（`kubectl logs` 报 `timed out waiting for the condition`）。

同一份代码的注释里其实记过近亲事故（`_job_state`：*"3 quota-starved Jobs read unknown forever
and their policy_runs rows dangled in running"*）——当时只修了解析器，没修"unknown forever"。

## 本次做的两件事

### 1. 手动关掉那条悬着的行（止血，已执行）

```sql
-- 先取证；age 18768s ≈ 5.2h，执行侧上限 10800s = 3h ⇒ 早已不是"还在跑"
SELECT id, policy_id, status, started_at, finished_at, detail
FROM policy_runs WHERE id = 2383;          -- running / NULL / ''

-- 条件更新：只在它仍是 running 时改，避免盖掉并发写者的结果
UPDATE policy_runs
SET status = 'stalled', finished_at = now(),
    detail = 'stalled (manual repair, 2026-11-13): row open 18768s vs the executor ceiling
              activeDeadlineSeconds=10800s; Job unresolvable since ttl/backoff reclaim, so
              poll() could only return unknown and the completion probe never closes unknown.
              Closed so per-policy single-flight stops blocking policy 2659.
              Yield unknown, NOT zero.'
WHERE id = 2383 AND status = 'running';
```

结果：`rows updated: 1`；policy 2659 的 open run **0 条**，全库 open run **0 条**。
产出数保持 `NULL`——**不许写成 `success`/`zero_yield`，那会把"执行器没说话"伪装成
"跑了但没数据"**，而 `zero_yield` 正是"抓到了没写进来"的唯一线索。

### 2. 把修复合进 main（治本，已合并 `cc8607e`）

`fix(dispatch-stall-reaper)`：

| 改动 | 为什么 |
|---|---|
| 新增终态 `stalled` / `skipped` | 前者表达"执行器没说话"，后者表达"被单飞挡住"——两种都必须是可查记录 |
| `policy_run_max_age_seconds()` **派生**自 `_active_deadline_seconds()`（+ 一跳 900s） | 两处各写一个数字迟早漂移：判得比执行侧紧 ⇒ Job 活着行被判死（**双跑**）；判得松或不判 ⇒ Job 死了行永不关（**本事故**） |
| 探针三处回收：无 `job_ref` 超龄 / `poll` 返回 `unknown` 超龄 / lease 队列未排空超龄 | 让"没关的行"自我痊愈，而不是人工发现 |
| **`poll` 返回 `running` 一律不回收** | 那是 Job 系统在说"它还活着"；误收一个真在跑的任务会双跑，比晚收更坏 |
| 单飞跳过落 `skipped` 行（含被哪条 open run 挡住及起始时间） | 日志随 pod 回收消失，这是已被实测证过的丢证据方式 |
| panel runs 视图 + `policy_runs` 工具补 `stalled`/`skipped` 筛选 | 写进去了但界面过滤不到，等于还是看不见 |

测试：`tests/test_reconciler_stall_reaper.py` 15 条（含阈值→0、阈值→无穷两条破坏实验，
以及一条因果证明"skipped 只在真的被挡住时出现"）；控制面回归 77 passed。

## 周一（2026-11-16）该看什么

`17 18 * * 1-5` 的下一次触发是周一 18:17 CST（11-14 是周六）。

```sql
-- 1) 那条行是否仍是终态，且 detail 里写着判据
SELECT id, status, started_at, finished_at, left(detail, 80)
FROM policy_runs WHERE id = 2383;

-- 2) 周一 18:17 之后有没有新行（有 = 派发恢复；没有 = 还有第二道门）
SELECT id, status, started_at, finished_at, left(coalesce(detail,''), 100)
FROM policy_runs WHERE policy_id = 2659 ORDER BY started_at DESC LIMIT 5;

-- 3) 数据有没有前移
SELECT MAX(o.date) FROM semantic_observations o
JOIN concepts c ON c.id = o.concept_id
JOIN entities e ON e.id = o.entity_id AND e.entity_type = o.entity_type
WHERE c.code = 'price.close' AND c.entity_type = 'stock';
```

判据分开读，别混：

- **有 `skipped` 行** ⇒ 还有别的 open run 在挡（看 `blocked_by_run` 指向谁）。
- **新行 `failed` 且 detail 含 `rate limited by eastmoney`** ⇒ 出口 IP 仍被封，
  处置见 `docs/ops/astock-daily-ingest-rate-limit.md`；**不是**本修复的回归。
- **新行 `success` 但观测仍停在 09-18** ⇒ 抓到了没写进来（写路径/口径映射），
  那是另一个问题，归因时看 `zero_yield`。

## 一个必须一起记住的判据教训

同一天我先用「`fetch_log` 近 60 天尝试 0 次 ⇒ 从未纳入派发」下过结论，**是错的**：
A 股价格走 `bulk_ingest_astock_daily.py` 批量路径，**不写 `fetch_log`**。
"每天在跑但每次失败"与"根本没人派发"在计数上完全同形。

判一条链路在不在跑，按层问，别用单一计数代理：
**策略层 `crawl_policies` → 运行层 `policy_runs` → 抓取层 `fetch_log` / 产出层 `semantic_observations`**。
任何一层的"零"都要先问"这条路径记不记这笔账"。
`fd-quant/scripts/decision_loop_probe.py --attribution` 已把这三层做成一条只读命令。
