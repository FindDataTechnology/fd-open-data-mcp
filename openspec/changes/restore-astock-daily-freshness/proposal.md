# Proposal

## Why

`fd-paper-trading` 的全部下单路径今天被一条闸拦死，拦得对但拦的原因没人修：全市场个股收盘价
停在 **2026-11-13**（`semantic_observations_all` 里 `price.close` 最大日期，5198 个实体，
此后逐日归零），ETF 停在 11-20。任何用户（含全新用户、全新账户）市价买入都是：

```
600000 → 400 {"code":"stale_quote","message":"Quote for '600000' is 12.4 days old (max 4.0)"}
510300 → 400 (5.4 days)          000300 → 400 no_quote（指数族全库零观测）
```

上游闸今天实跑判红，且判得准：`policy 2659 astock-daily-increment` 最近一次
`failed`，detail 是 `deadline exceeded after 10800s; … failed before the runner checked in;
pod 已回收`，紧接着一条 `[opaque_reason]`——**重跑救不了它，要先让它留下可判原因**。同一天
还有五条基金策略判 `repeated_zero_yield`（连续 5 次终态零产出，连一次 flush 都没上报）。

这个 change 只做一件事：**让价格链重新产出，并让「产不出」有可判原因**。不谈交易侧、不谈看板。

## What Changes

- **让 2659 的失败可判**：当前的 `activeDeadlineSeconds=10800` 在整轮远未跑完时把 Job 杀掉，
  而 `restartPolicy: OnFailure` + `backoffLimit` 让 pod 被回收、`policy_runs.detail` 只剩
  「pod 已回收」。改为「保留现场」的调试派生 + 让执行体在**被杀之前**自己写出进度与当前符号，
  使下一次失败能落成 `deadline exceeded at symbol X, batch N/M`，而不是 `opaque_reason`。
- **补一次真数据**：对「策略 universe ∪ 一个可交易标的集」做定点 backfill，把 `price.close`
  水位推到最近一个交易日；指数的 `index-seed` 口径同样补到可用（现在全库零观测，基准与
  `000300` 报价都因此为空）。
- **基金的零产出**：五条 `fund-*` 策略连续零产出，按同一手法留痕并定位（是上游不给，还是
  首次上报前就结束）。
- **闸的判据补一条**：`policy-freshness` 对 `opaque_reason` 今天的处理是「重跑救不了它，
  要先补留痕」——本 change 把「补留痕」做成代码而不是待办。
- **不做**：不放宽交易侧的 `stale_quote`（那是保护，不是障碍）；不改 provider 的钉源纪律；
  不引入新数据源。

## Capabilities

### New Capabilities

（无——本 change 全部落在既有能力的 requirement 上。）

### Modified Capabilities

- `price-on-demand-fill`: 增加「补数的进度与当前标的在进程被杀前落下」一条——现有 spec 只要求
  失败被标注为 degraded，没要求「为什么失败」可判；opaque detail 正是这个缺口。
- `crawl-job-lifecycle`: 增加「deadline 终止的 run 必须带上被杀时的进度」，把今天的
  `deadline exceeded after 10800s` 从「有时长」升级为「有位置」。
- `price-active-pool`: 明确预热的可观测计数在**首轮 flush 之前**也必须有值（零产出的判据
  现在只能看到 0，看不到「卡在哪一步」）。

## Impact

- 代码：`scripts/bulk_ingest_astock_daily.py`（进度落盘 / 信号处理）、
  `fd_open_data_mcp/refresh/reconciler.py`（deadline 关闭时补 detail 位置）、
  `scripts/check_policy_freshness.py`（若 opaque 的判据要跟着收紧）。
- 数据：一次受控 backfill 写入 `semantic_observations_all`（insert-only，不改历史行）。
  写前查 `guangzhou-xinru` 大盘水位（86% 满的既定约束）。
- 运维：`docs/astock-daily-increment.md` 增「保留现场」与「进度落盘」两节；`policy 2659` 的
  Job template 参数（如需）在 `crawl-planner` 侧。
- 下游：`fd-paper-trading` 的 `stale_quote` 会在数据追上后自然放开（不需要改它）；指数族
  有数据后 `000300` 的 `no_quote` 也会消失。
