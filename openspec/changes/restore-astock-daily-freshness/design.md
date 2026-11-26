# Design

## Context

See `proposal.md` - Why. 现场事实与判据：

| 事实 | 判据 |
|---|---|
| 个股收盘最大日期 2026-11-13 | `select max(date) from semantic_observations_all where concept_id=236 and source_used='astock_daily'` → `2026-11-13`；逐日计数 11-06..11-13 各 ~5190，之后无行 |
| ETF 停在 2026-11-20 | 同口径 `source_used` 取 fund 族 → `2026-11-20`；provider 实测 `510300` 报价 `bid 4.01` |
| 指数族零观测 | provider 构造时日志 `no price.close concept for entity_type=index`；`get_bars("000300")` 返回 0 根 |
| 2659 最近一次是 deadline 杀 | `policy-freshness` 实跑输出：`deadline exceeded after 10800s; job cheap9/crawl-policy-2659-20261124102203 failed before the runner checked in; pod scraw/…`，紧随 `opaque_reason` |
| 单条 stderr 型证据仍可能消失 | `restartPolicy: OnFailure, backoffLimit: 1` ⇒ 重试用尽后 Job 控制器删 pod（`docs/astock-daily-increment.md` 2026-11-12 事故） |
| 限流是多 provider 故障转移 | `PROVIDER_ORDER=("eastmoney","tencent","sina")`，限流零等待换腿，全拒才抛（`tests/test_astock_rate_limit_backoff.py` 头注 2026-11-15 订正） |
| 写库是高水位分片 | canonical `guangzhou-xinru` 86% 满；数据分片 `guangzhou-xinru2/3` |

## Goals / Non-Goals

**Goals**
- 下一次 2659 失败时，`policy_runs.detail` 能回答「跑到哪、卡在哪个符号、是不是上游全拒」。
- 价格水位回到最近一个交易日（个股 + ETF + 指数），交易侧的下单门自然放开。
- 补数过程对它自己的写量有界（分片串行、每片一批、写前查余量）。

**Non-Goals**
- 不保证「明天还在」：如果 e2e 结论是上游对 cheap9 出口封禁，本 change 只能补历史，**不能**
  让增量长期自愈；那种情况要在 `tasks.md` 里如实记成「需要换出口/换源」，而不是宣称修好。
- 不放宽 `stale_quote`，不改 provider 钉源纪律。
- 不动基金/宏观/其它无关策略的采集频次。

## Decisions

### D1 deadline 从「只有时长」升级为「有位置」：进程自己落进度

两条腿，互为兜底：

1. **进程内**：`bulk_ingest_astock_daily.py` 每完成一个符号写一行进度（符号、批号、已写行数）
   到 `policy_runs.detail`（或它旁边的 progress 列/表——按现有 schema 选最小改动），并 `flush`；
   捕获 `SIGTERM`（K8s 在 deadline 时先发 TERM，默认 30s grace）时把当前符号写进 detail 再退出。
2. **关闭时**：`reconciler` 在把 run 关成 `deadline_exceeded` 时，把已有 detail 与「最后一个
   进度符号」拼成可判原因；detail 仍是空的才写 `opaque_reason`。

- 备选：只加 `terminationGracePeriodSeconds` 让进程有更多时间。否决：那是把窗口拉长，不改变
  「证据没落盘」这个根因。
- 备选：只做调试派生（保留 pod）。否决：那是人的动作，不是机器的；今天已证明没人做。

### D2 补数走既有 `fill_gaps`，不手搓全量 Job

`fd_open_data_mcp.fetch.gapfill.fill_gaps(session, concept_id, entity_type, entity_id, start, end)`
已经是唯一补数入口（insert-only、已探明无数据的区间只问一次、失败标 degraded）。故补数用一个
**受控 runner**（既有 `active_pool.prewarm` 或等价调用）对一个小 universe 跑，而不是新写一条
「拉全市场」的路径。

universe 的构成（按顺序取并集，有上限）：
- `fd-paper-trading` 的策略 universe（现在 3 条 RUNNING 都是 `600000`）；
- 可交易标的集：`600000`、`600036`、`600519`、`000001`（个股）与 `510300`、`510050`、`159915`（ETF）；
- 基准：`000300`、`000905`、`000001`、`399001`（index 族，`index-seed` 口径）。

- 备选：直接重跑 2659 全市场。否决：它今天正是被 3h deadline 杀的，重跑等于重演；而交易侧
  只需要一个小集合就能解锁。

### D3 指数的「零观测」要先分清是「没有源」还是「没有数据」

`index-seed` 声明存在但全库零观测。必须先在库里确认：`entities` 里有 `index` 实体吗？
`concepts` 里 `entity_type=index` 的 `price.close` 存在吗？两者缺一，补数就是空转。
答案决定 action：缺实体 ⇒ 先 seed 实体；缺概念 ⇒ 先注册概念；都有 ⇒ 才是补数。

### D4 基金零产出沿用同一手法

五条 `fund-*` 的判词是「计划 N 格，但连一次 flush 都没上报（pod 在首次上报前就结束）」。
D1 的进度落盘同样覆盖它们（同一个执行体基类或同一份 helper），所以本 change 的代码改动
天然让基金侧也变成可判。

## Risks / Trade-offs

- **补数写爆分片盘** → 写前查 `guangzhou-xinru` 水量（既定 86% 约束）；按源/分片串行、每批
  有上限；backfill 用既有 insert-only 路径，不产生历史行重写。
- **上游继续封禁** → e2e 若证实是出口封禁，则本 change 交付「补到 11-2x 的历史 + 可判原因」，
  并显式记下「增量自愈需要换出口/换源」。**不**把「补完一次」说成「修好了」。
- **SIGTERM 不一定会到**（例如 `OOMKilled`） → 进度落盘是每符号一次，天然覆盖 OOM 形态；
  deadline 形态靠 TERM，OOM 形态靠最后一次落盘。
- **改 `reconciler` 的关闭逻辑** → 它同时被单飞与容量计数依赖；改动只动 detail 合成，不动
  状态机迁移，并用既有 `tests/test_visibility_scan.py` / `tests/test_policy_freshness_gate.py`
  回归。

## Migration Plan

1. 代码（D1/D3）→ 单测（`tests/test_astock_rate_limit_backoff.py` 新增进度落盘用例；
   `tests/test_policy_freshness_gate.py` 新增 `deadline_exceeded` 带位置的形态）。
2. 出片 → 滚 `fd-open-data-mcp`（与 `scraw` 执行镜像）→ 在 pod 内跑一次受控 backfill（D2）。
3. 复核：`select max(date)` 到最近交易日；`policy-freshness` 该策略转绿或转成带位置的失败。
4. 回滚：代码回滚到上一 tag；数据是 insert-only 补数，无需回滚（多出的行是真实数据）。

## Open Questions

- 指数族的缺的是实体还是概念（D3），要查库才知道；不阻塞其它任务。
- 2659 的 10800s 是否要按 universe 量级调大——**取决于 D2 的 e2e 读数**；先让它可判，再决定
  是否调参，而不是先调参。
