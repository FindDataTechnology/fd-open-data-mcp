# 容量普查报告：semantic_observations 与爬取链路（P0）

- 日期：2026-11-04
- 目标库：生产 `fd_open_data`（cheap 集群，100.64.0.3:30432，fd-open-data-mcp pod 现役）
- 变更：add-data-stream-engine Task 1.1（只读测量，未写任何生产数据）

## 1. 总量与形态

| 指标 | 值 |
|---|---|
| semantic_observations 总行数 | **6,498,779** |
| 表体积 | 1,647 MB（heap 822 MB + index 825 MB） |
| 首行入库 | fetched_at 2026-07-31（加载期 ~3 个月） |
| distinct concepts | 355 |
| distinct (entity_type, entity_id) | 15,072（entity_type 9 种，stock 实体 9,748） |
| distinct source_used | 28 |
| distinct observation date | 5,491（月桶 561 个） |

## 2. 关键事实一：granularity 生产实态是 6 档，不是 3 档（schema 漂移）

models.py 注释与迁移口径为 `day | month | year`，实测：

| granularity | 行数 | 占比 |
|---|---|---|
| **quarter** | **4,149,814** | **63.9%** |
| day | 2,056,516 | 31.6% |
| year | 284,795 | 4.4% |
| month | 5,384 | 0.08% |
| hour | 2,237 | — |
| week | 33 | — |

财务类概念（financials.*）以 quarter 粒度落库（concepts.frequency=yearly 是刷新节奏，行粒度是季报期），raw-SQL 写入方绕过了模型层注释的三档口径。

## 3. 关键事实二：date 列已污染，违反 canonical YYYY-MM-DD 契约

- `min(date) = '1960'`（裸年份字符串）
- `max(date) = 'LATEST'`（哨兵值进了数据行）

models.py 注释的规范化（year→12-31、month→01）只在上层写入路径生效，raw 写入方产生了非规范值。**直接影响 P1 月分区设计：不能直接对 date 做 RANGE 分区**，须先治理或用派生键。

## 4. 增量形态：基线滴灌 + 百万级回填脉冲

按 fetched_at 分周：

| 周 | 行数 | 概念数 | 形态 |
|---|---|---|---|
| 08-31 | 1,012,382 | 23 | 回填波 |
| 09-07 / 09-14 | 425K / 406K | 193 / 47 | 回填波 |
| 10 月各周 | 1K–11K | ≤88 | 基线滴灌 |
| **11-02 当周** | **4,153,828** | 32 | **单日 4.15M 脉冲（11-03）** |

11-03 脉冲构成：financials.* 八个概念各 ~212K 行（≈5,200–8,300 家上市公司 × 历年季报期），即覆盖扩展波（expand-crawl-coverage）一次回填。

- 基线日增：~150–4,200 行/日（≈0.02–0.05 msg/s）
- 脉冲日增：**4.15M 行/日**（≈48 msg/s 均值；按波次数小时内完成估算，峰值数百–2000 msg/s）
- 波次台账（coverage_waves）：10 条；113（stock/daily）、115（country/yearly）**paused**，119/120 verifying——暂停波恢复即再有同量级脉冲

## 5. 热点概念

| concept | 行数 | 实体数 |
|---|---|---|
| aum（quarterly） | 831,748 | 322 |
| financials.accounts_payable | 287,343 | 8,339 |
| financials.accounts_receivable | 283,097 | 8,136 |
| financials.debt_ratio | 262,939 | 7,681 |
| financials.net_income | 214,150 | 5,519 |
| financials.revenue | 214,108 | 5,594 |
| （…其余 financials.* 各 ~211K） | | |

## 6. 执行链路体量与索引现状

- fetch_log 2,240 行（采集面很轻）；policy_runs 823；crawl_runs 4,190；enabled 策略 10 条
- semantic_observations 仅 3 个索引：pkey(id)、ix_concept_id、uq_sem_obs(concept_id, entity_type, entity_id, date, granularity, source_used)
- **fetched_at 无索引**：面板 staleness 检查与本普查的分日统计都在全表扫描
- date 在 uq_sem_obs 中间位：日期范围扫描无法高效走该索引

## 7. 读路径现状复核（102s 问题的真解药已被验证）

- 102s 聚合 = 首次冷启动时对 6.5M 行 GROUP BY concept；现役 `concept_coverage`（355 行/概念粒度，小时刷新 + advisory lock 单飞）已将其消除
- **实测 rollup 压缩比：DISTINCT(concept_id, entity_type, entity_id, date, granularity) = 6,498,779 = 总行数，即零压缩**——生产中每个观测点恰好只有一个 source_used（多源共存特性未物化）。「concept×entity×day 粒度 rollup」在此数据形态下退化为 1:1 复制表，无聚合价值
- 结构性修法的正确粒度：**概念级汇总 rollup（355 行）在写入路径上增量维护（O(Δ)）**，替代小时级全表扫描刷新（现刷新 ~100s，随表线性增长：100M 行 → ~25 分钟/次，逼近小时节奏上限）

## 8. 对设计的校准结论（喂给 Task 1.2 / design 附录）

1. **流面容量**：按已观测最大脉冲 4.15M/日 + 暂停波恢复的富余，单流 Redis Streams（≥10K msg/s 实测能力）余量 >5×；payload 内联可行（观测行 <1KB），MinIO 载荷引用仅在批灌场景启用
2. **分区键**：date 列须先治理（'LATEST'/'1960' 等非规范值），或分区键用规范化派生表达式 + default 分区收容；分月桶 561 个 → 历史跨度大，按月分区的分区数预算 ~600（需按治理后跨度复核）
3. **rollup 重定粒度**：concept×entity×day → 概念级汇总（增量维护）；面板读路径从「小时全表刷新」变为「写入即更新」
4. **补索引**：fetched_at 必须补索引（无论分区与否），现有多处全表扫描路径依赖它

## 9. 复现命令

```bash
pg=$(kubectl get secret fd-mcp-env -n fd-prod -o jsonpath='{.data.FD_OPEN_DATA_MCP_DATABASE_URL}' | base64 -d)
psql "$pg" -c "SELECT granularity, count(*) FROM semantic_observations GROUP BY 1;"
psql "$pg" -c "SELECT min(date), max(date) FROM semantic_observations;"
```

## 附录：date 列非规范值 pattern 清单（Task 2.1，2026-11-04）

| pattern | 行数 | distinct 值 | 样例 | 治理映射（分区派生桶 `obs_month`） |
|---|---|---|---|---|
| canonical_ymd | 6,471,852 | 4,799 | 1986-02-01 ~ 2026-12-31 | substr(date,1,7) |
| bare_year（裸年份） | 23,667 | 67 | '1960' ~ '2026' | 'YYYY-12' |
| iso_timestamp（小时截断） | 2,237 | 459 | '2026-10-06T02…' | 'YYYY-MM'（前 7 位） |
| bare_month（裸月份） | 1,015 | 158 | '2013-05' ~ '2026-10' | 'YYYY-MM' |
| ISO 周 | 7 | 7 | '2026-W35' ~ '2026-W41' | 该周任一日所在月（pharm 周粒度） |
| 哨兵 'LATEST' | 1 | 1 | datacommons country 人口，day 粒度 | default 分区（无月桶） |

**裸年份行的真实身份**（18,621 行 stock/day 抽样核实）：cnreport 财报波写入缺陷——年度报告值被写成 `granularity='day', date='2025'`（例：financials.revenue @ cnreport）。属业务语义缺陷，**不在 P1 改写存量**（改 granularity/date 有 uq_sem_obs 冲突与读路径破坏风险）；处置：
1. P1 止新增：写入边界（模型层规范化已有 year→12-31 先例）补 cnreport 路径的 canonical 化；
2. 存量 18,621 行的语义修复（day→year 改粒度）另立数据质量小 change，与本 change 解耦；
3. 分区用派生桶 `obs_month`，不改任何存量 date/granularity 值——零 uq 冲突、零读路径风险。
