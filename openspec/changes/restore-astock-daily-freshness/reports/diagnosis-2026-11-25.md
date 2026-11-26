# 诊断与定点补数（2026-11-25 实测）

## 1. 2659 失败史（policy_runs，生产库）

| run | started | 状态 | 耗时 |
|---|---|---|---|
| 2534 | 11-25 10:22 | running | — |
| 2523 | 11-24 | failed | 180.0 min |
| 2489 | 11-23 | failed | 195.0 min |
| 2461 | 11-20 | failed | 195.0 min |
| 2450 | 11-19 | failed | 180.0 min |
| 2439 | 11-18 | failed | 195.0 min |
| 2428 | 11-17 | failed | 195.0 min |
| 2413 | 11-16 | failed | 180.0 min |
| 2386 | 11-14 | stalled | 35.2 min（人工停手：东财对 cheap9 出口限流） |
| **2323** | **11-09** | **success** | **45.0 min** |

11-01 以来：`failed 21 / stalled 2 / success 1 / running 1 / zero_yield 1`。
**最后一次成功是 11-09；此后每个交易日都以 deadline（10800s = 180min）被杀。**

## 2. 根因：不是数据变了，是耗时爆炸

从 paper-trading pod 直测三条 provider 腿（同一 start/end）：

```
eastmoney  ERR ConnectionError: RemoteDisconnected('Remote end closed connection')
tencent    rows=8  (11-24 收 10.69)   ← 可用
sina       rows=8  (11-24 收 10.69)   ← 可用
```

东财出口被封（`RATE_LIMIT_MARKERS` 已含 `RemoteDisconnected`，脚本正确识别并切腿）。
但备源**慢**：

- 原始端点 `proxy.finance.qq.com/.../newfqkline/get`：**0.2s**
- 经 akshare `stock_zh_a_hist_tx`：**7.4s/符号**（内部按年循环 + pandas 归一）
- 外推 5203 个符号：**10.6 小时 ≫ 3 小时 deadline**

> 判据：`avg 7.36s/symbol → 10.63 h projected`（5 个符号实测）

结论：**2659 是容量问题**，不是「上游没数据」。它每个交易日都会跑满 deadline 被杀，
所以 detail 永远只有 `deadline exceeded after 10800s`。

## 3. 指数族：缺的是「注册 + 数据」，不是「抓取失败」

```
entities where entity_type='index' → 17 行（含 000300 / 000905 / SHCOMP …）
concepts where entity_type='index' and code='price.close' → 0 行   ← 缺概念族
semantic_observations_all for those concepts → 0 行
```

provider 日志自证：`astock provider: no price.close concept for entity_type=index`。
仓库里**已有**一次性导入通道 `scripts/import_index_seed.py`（CSV → `source_used='index-seed'`），
它会在导入时创建概念族；所以只差一份快照 CSV 的落地，**不需要新代码**。

## 4. 定点补数（已执行）

用仓库自带的 `bulk_ingest_astock_daily.py`（不是手搓 Job），走它的 hfq 校准写入路径：

```
--symbols 600000,600036,600519,000001,601318,600900 --start 2026-11-14 --end 2026-11-25
→ done: 240 rows, 0 fetch failures; providers: tencent=6; skipped eastmoney
```

ETF（fund 族，`source_used='akshare'`，不复权）用 `ak.fund_etf_hist_sina` 补 11-21..11-25：

```
510300 / 510050 / 159915 各 12 observations over 2 dates，写 ft_sem_obs_xinru3_w（含 date_ts 分区键）
```

## 5. 验收（改前 → 改后）

| 项 | 改前 | 改后 |
|---|---|---|
| 新用户市价买 600000 | `400 stale_quote`（12.4 天） | **202 FILLED @10.33** |
| 600036 | `400 stale_quote` | **202 FILLED @41.32** |
| 510300 | `400 stale_quote`（5.4 天） | **202 FILLED @4.04** |
| 510050 / 159915 | `400 stale_quote` | **202 FILLED @2.90 / @2.30** |
| 000300 | `400 no_quote` | 仍 `no_quote`（指数族待 seed，见 §3） |
| 合成闸结论 | `blocked` | **`degraded`** |
| 执行层 | 3× `data_stalled` | **`tradable`** |
| 3 条 RUNNING 的 `last_bar_date` | 停在 2026-11-13 | **推进到 2026-11-25（自己跑的，未重启）** |

剩余 `degraded` 的 4 条全是已声明缺口：`007355`（场外基金无交易所价）、`AAPL/MSFT/NVDA`（不在本线范围）。

## 6. 未做与必须说清的

- **增量仍未自愈**：明天 2659 还会跑满 deadline 被杀（容量问题未解）。本 change 只证明
  「补历史可行且交易面能解锁」。要真正修复需二选一：① 把 2659 拆片并行/提高 deadline；
  ② 把备源换成批量端点（原始腾讯端点 0.2s，是 akshare 封装的 37 倍差距）。属另立 change。
- 东财出口封禁本身未处理（它已被脚本正确切腿，不再是阻塞）。
- 指数族仍未 seed（基准相对指标与 `000300` 报价仍不可用）。
