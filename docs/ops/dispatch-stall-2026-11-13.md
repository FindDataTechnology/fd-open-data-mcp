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

### 2. 把修复合进 main（治本的一半，已合并 `cc8607e`；另一半「出片」见下一节）

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

## 但修复还没出片（2026-11-14 上午补；当天 05:00Z 已出片，见下）

> **状态更新（2026-11-14 05:00Z 实测）：已经出片了。** `crawl-reconciler` 的镜像从
> `sha-00817fb` 换到了 **`sha-2f2ccd2`**，进镜像 grep：`policy_run_max_age_seconds` **4** 处、
> `stalled` **12** 处；第一轮用新片的 Job 是 `crawl-reconciler-29910517`（04:52Z）。
> 下面这一节保留原样，因为「合进 main ≠ 出片」这条判据仍然成立，而且它解释了
> 为什么当时看起来一切都好。

**上面第 2 节写「合进 main ＝ 治本」，这句话现在只对了一半：合进 main 只是治本的一半，
另一半是出片。**（写这一节的时刻，运行中断行的那份代码里没有这个修复。）

判据（只认进镜像 grep，不看 git 有没有那笔）：

```bash
# 真实路径在 site-packages，/app 下只有源码树——路径猜错时"没找到"与"不存在"同形
kubectl --context xinru -n fd-master run probe --rm -i --restart=Never \
  --image=ccr.ccs.tencentyun.com/finddata/scraw-fd-open-data-mcp:sha-00817fb \
  --command -- sh -c 'grep -c policy_run_max_age_seconds \
    /opt/venv/lib/python3.12/site-packages/fd_open_data_mcp/refresh/reconciler.py'
```

两个在跑镜像（`xinru/fd-master` 的 `crawl-reconciler` = `sha-00817fb`、
`cheap9` = `sha-eb366d0`）实测 **0**；`stalled` 同样 **0**。而 `crawl-reconciler`
每 15 分钟就在跑（`kubectl -n fd-master get po | grep crawl-reconciler` 能看到
40/25/10 分钟前那几轮）——**跑的是没有自愈的那份代码**。

根因在 pin：`scraw-fd-open-data-mcp/Dockerfile` 第 151 行

```
ARG FD_ODM_REF="9221ec2647b4e8ff89f0035561efdb0c996b6a33"
```

`9221ec2..origin/main` 共 **8 笔**，其中两笔带行为，**必须一起进**：

| commit | 带的是什么 | 不进的后果 |
|---|---|---|
| `cc8607e` | reconciler 自愈（本页第 2 节） | 悬着的 `running` 行继续永久堵住派发 |
| `2549fcb` | `astock_daily/price` 补保留契约（W/1826）并**进 seed 矩阵**（`scripts/audit_retention_contracts.py` + `tests/test_seed_never_narrows.py`） | 下次 `seed --overwrite` 把库里那条已有契约当"多余"删掉，16GB / 48M 行的家族回到**审计与淘汰都看不见**的状态 |

后一条容易被漏掉，因为症状不在派发线上：它只在**下一次 seed** 现形，而且现形的形态是
"契约少了一条"，不是报错。所以抬 pin 的验收要同时看两个数：`policy_runs` 出现
`stalled`/`skipped`，以及保留契约行数 **103 → 104**。

> **订正（2026-11-14）：「103 → 104」这条判据已经用不上了。** 现在
> `select count(*) from data_retention_contracts` 就是 **104**（`2549fcb` 那一行已在库里），
> 所以「104」既可能意味着「同步成功」也可能意味着「本来就 104」，**没有区分力**。
> 要看的是那一行本身在不在，而不是总数：
>
> ```sql
> SELECT source, family, retention_class, retention_window_days, re_fetchable
> FROM data_retention_contracts WHERE family = 'price' AND source = 'astock_daily';
> -- 期望：W / 1826 / re_fetchable = true
> ```

出片是**三处**，少一处就等于没出：

1. 镜像 `FD_ODM_REF` 抬到含这两笔的 sha（重建 `scraw-fd-open-data-mcp`）；
2. 派发器 `crawl-reconciler` 的镜像 tag 换到新片（xinru 与 cheap9 两个集群都要换）；
3. `clusters.image` + `SCRAW_K8S_IMAGE`——**改 `fd-scripts` ConfigMap 对已派发的 Job 无效**。

**2026-11-14 05:00Z 复核：三处都已到位**（另一条线做的，逐处验过，不只看了 git）：

| 处 | 实测 |
|---|---|
| 1. pin | `scraw-fd-open-data-mcp/Dockerfile` 的 `FD_ODM_REF` = `82831c9…`（含 `cc8607e` 与 `2549fcb`），仓 head 与线上片 tag 同名：`sha-2f2ccd2` |
| 2. 派发器 | CronJob 镜像 = `scraw-fd-open-data-mcp:sha-2f2ccd2`；进镜像 grep：`policy_run_max_age_seconds` **4**、`stalled` **12**、`fetch/retention_bulk.py` **在**；第一轮用新片的 Job = `crawl-reconciler-29910517`（04:52Z） |
| 3. 分片 | `clusters.image` 四个集群（america/baidu/cheap/cheap9）**全是** `sha-eb366d0`，`SCRAW_K8S_IMAGE` 同片；该片进镜像 grep `retention_bulk` **在**、`retention.py` **在**。只有 `cheap9` 是 `enabled=true` |

**「为什么没有当场推」这段保留**：当晚确实不宜单方面推——`crawl-reconciler` 是全平台所有
策略的唯一控制面，坏片的爆炸半径是整支舰队（不是某一条源），出片要单独一个窗口、一次只
动一个集群、跑一轮看清 `policy_runs` 再动第二个；这条线也有 owner。

### 还剩一处：`fd-scripts` ConfigMap 里 4 份脚本落后仓

出片之后，写入侧镜像都带 `retention_bulk` 了，但**脚本正文仍从 `fd-scripts` 取**，而那里
5 个有出处的键里 **4 个比仓里少约 1KB**（缺直写路径的留存收尾、其中两份还留着已被仓里
删掉的 dev DSN 兜底）。判据与它为什么「没有清单＝对所有按清单扫的闸隐形」，见

    python3 fd-infra-deploy/tools/check_fd_scripts_configmap.py

**同步之前先看这四族的契约**（2026-11-14 查 `data_retention_contracts`）：

| 族 | 被哪些脚本写 | 契约 | 窗口 | `re_fetchable` |
|---|---|---|---|---|
| `financials` | `bulk_ingest_financials_aggregate.py`、`bulk_ingest_stock_financials.py` | **T** | 30 天 | true |
| `nav` | `bulk_ingest_fund_nav_daily.py` | **T** | 30 天 | true |
| `price` | `bulk_ingest_astock_daily.py` | W | 1826 天 | true |

也就是说：同步之后这三族**第一次真的按契约收尾**，而 `financials` / `nav` 是 **T 类（只留当期、
30 天窗）**——那等于把 30 天以前的行删掉（补一条 `revision_kind='batch'` 的痕迹）。契约上这么写，
且两族都 `re_fetchable = true`（删了能重取），但这仍是**一次真实的批量删除**，不是"补个补丁"。
所以同步要按族分开做、每族先记下删前行数与删后行数；`price` 那一族窗口 1826 天，实际删不动多少。

**同步的前置条件现在满足了**（目标镜像自带 `retention_bulk`，否则会 ImportError），但同步
等于给这四族**新开一条按契约删历史行的路径**——所以它要按族想清楚再动，别顺手抄。顺序：
先跑上面那道闸确认差异只在预期处 → 一次只同步一族 → 实跑一轮看 `policy_runs` 与行数变化。


## 周一（2026-11-16）该看什么

`17 18 * * 1-5` 的下一次触发是周一 18:17 CST（11-14 是周六）。

**第 0 步先确认片换了**，否则下面三条都会被读错：

```bash
kubectl --context xinru -n fd-master get cronjob crawl-reconciler \
  -o jsonpath='{..image}'
# 2026-11-14 05:00Z 实测已是 sha-2f2ccd2（含修复）。若又回到 sha-00817fb
# ⇒ 修复不在运行中，「没有 stalled/skipped 行」只能读成「写这些行的代码还没上」。
```

**第 0.5 步：别把那条手工行当成 reaper 的产出。** 全库现有**唯一**一条 `stalled`
是 `id=2383`，`detail` 以 `stalled (manual repair, 2026-11-13)` 开头——那是人工关的。
所以「出现了 stalled」这个判据必须带 id 界限：

```sql
-- 已经拿到过的基线（2026-11-14 05:00Z）：success 26 / failed 11 / no_op 11 /
-- zero_yield 4 / redundant 3 / stalled 1（就是 2383），open_runs = 0。
SELECT status, count(*) FROM policy_runs
WHERE started_at > now() - interval '4 days' GROUP BY 1 ORDER BY 2 DESC;

-- reaper 真的开火了吗？只认 id > 2383 的新行（2383 是人工那条）
SELECT id, policy_id, status, started_at, left(coalesce(detail,''), 90)
FROM policy_runs WHERE id > 2383 AND status IN ('stalled','skipped')
ORDER BY id DESC LIMIT 10;
```

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
