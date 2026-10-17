## Why

交易侧所有价格依赖能力（`equity-valuation` 通道 B 前置检查、`strategy-weight-mode` 的多标的回测与纸面实盘、路线图 P1/P2/因子/轮动/产业链五个 pack）都要求"A 股某标的某区间的日线序列"。今天这条路只有两个断点：

- **本地 DAAS 只有 600519 一只**（实测：`scraw_*` 66 张表里 A 股 1 张，6,022 行 / 2001-08-27→2026-10-16）。其余标的取不到，估值套件只能走"中止估值"分支。
- **中央库里有过全市场抓取，但不是历史序列**（2026-10-17 实测 xinru-master `semantic_observations`，`source_used='akshare'`）：`price.close` 覆盖 3,533 只但只有 **2026-06-09→2026-09-18 约 3 个月**、每只中位数 **44 行**；带完整 OHLC 的仅 868 只各 100 行；`600519` 的 `price.close` **0 行**。够做截面统计，不够做 730 天回测。

用户已裁决（2026-10-17）：**不做全市场预灌**。理由不是"存储贵"——实测全市场全历史仅约 **0.74–1.84 GB**（57.8 字节/行），一台 40G 盘放得下——而是三件更贵的事：①全市场抓一轮 26–88 分钟，且 akshare 会限流瞬断；②每次回测实时抓会让结果不可复跑（上游一次修订，昨天的 run 就复现不出来）；③**前复权序列会随每次分红除权被上游整体重写**，缓存失效不是时间问题而是数据被改写。故采用**按需取数 + 本地留存**，并要求 **A（按需补数）与 B（活跃池预热）都做**。

**关键发现（决定了本变更的形状）**：`fd_open_data_mcp.fetch.dispatch.read_range` **已经是方案 A 的骨架**——它读缓存，缓存缺失或过期才抓一次并回写。而 `cache.is_stale` 对"已完结周期"直接返回 False（`_period_final`），所以**一次回测窗口读完之后，历史部分永不再抓**，只有当天那一格受 20h TTL 管。这正是"按需 + 留存"想要的语义，不需要另造一套。

因此本变更**不新建取数机制**，只补三个已证实的缺口（见 What Changes）。

## What Changes

- **缺口 1：部分覆盖不补数（正确性缺陷）**。`_read_range_one` 的既有注释自己写明：「partial-coverage detection is by staleness only. A range with some fresh-but-incomplete cached rows returns what's cached without re-fetching.」对价格序列这是**静默错数据**：假设库里只有 2026-01-05→01-20 的 600519（早期逐日读留下的），请求 2025-01-01→2026-10-17 会拿回那 12 天，**不报缺口也不补数**，回测就按 12 天算。本变更引入**覆盖度判定**：用**数据自身**推断（同一标的的已缓存日期集合即其交易日历，不引入外部日历——design D1 说明为什么），判定窗口边界与内部空洞，未完整则**只补缺失段**（不是重抓整段）。
- **缺口 2：`read_series` 的"不联网"契约与按需补数冲突**。既有 `observation-reading` spec 的 `read_series` 明确要求"without dispatching upstream"、"gaps are gaps, not surprise fetches"。方案 A 要联网。**不推翻该契约**（它保护的是"读工具不该偷偷花钱/变慢"），而是新增一个**显式请求补数**的入口：调用方声明 `fill_gaps: true` 时才补，默认仍不联网。这样"读"与"补"是两种意图，不是把读变成隐式抓取。
- **缺口 3：没有活跃池（方案 B）**。B 需要一个"哪些标的值得预热"的集合。本变更新增**活跃池**定义与预热动作：池 = 显式配置的自选 + 近期被请求过的标的（按请求频次），预热 = 对池内标的按配置的历史深度抓一次并落库。预热是**离线动作**（可定时、可手动），不在请求路径上。
- **数据指纹（让回测可复跑）**：落库的每个价格点已带 `fetched_at` 与 `source_used`；本变更新增**序列指纹**——对一个 (标的, 区间, 口径) 返回内容哈希，供 run 记录钉住"当时那份数据"。这不是新表，是既有行的确定性摘要。
- **不改变政策边界**：不在服务端目录注册价格类概念（沿用既有 `price.*` System-B 概念），不做实时行情，不做复权口径回填/迁移，不预灌全市场。

## Capabilities

### New Capabilities

- `price-on-demand-fill`: 按需补数——覆盖度判定（窗口是否完整）、缺失段补抓（只抓缺的，不重抓整段）、显式 `fill_gaps` 语义（默认不联网）、补数失败时的降级（返回已覆盖部分 + 明确缺口标注，绝不静默返回不完整序列）、以及序列指纹（内容哈希供 run 钉住）。
- `price-active-pool`: 活跃池预热——池的构成（显式配置 + 近期请求频次）、预热动作（按配置深度抓取并落库、幂等）、池的查询与维护，以及"预热失败不阻塞请求路径"的边界。

### Modified Capabilities

- `observation-reading`: `read_range` 的覆盖度判定从"仅按 staleness"改为"staleness + 覆盖完整度"（部分覆盖必须补或明确标注，不得静默返回）；`read_series` 增加可选的 `fill_gaps` 参数（默认 false，保持既有"不联网"语义）。

## Impact

- **代码**：`fd_open_data_mcp/fetch/dispatch.py`（`_read_range_one` 的覆盖度分支）、`fd_open_data_mcp/fetch/cache.py`（覆盖度/指纹辅助）、新增活跃池模块与 CLI 命令（`prewarm-prices` / `pool`）、`fd_open_data_mcp/mcp/server.py`（`read_series` 的 `fill_gaps` 参数）。
- **数据**：`semantic_observations` 新增价格行（按需与预热两条来源），沿用既有 `price.*` 概念与 `astock` 源；无新表。序列指纹是计算值，不落库（由调用方记录）。
- **依赖**：akshare（`ak.stock_zh_a_hist`，客户侧直取，需清空代理）——复用 `DAAS/fd-daas-mcp/daas-mcp/seed_astock_price.py` 已验证的取数与口径逻辑，不重写。
- **上游消费方**：`fd-paper-trading` 的 `FDOpenDataMarketDataProvider`（其 `get_daily_bars` 走 `read_range`）、`equity-valuation` 通道 B 前置检查、`fd-quant` 的计算面（纯计算，不取数，但回测的输入来自这里）。
- **不作承诺**：不做全市场预灌、不做实时行情、不做分钟级、不迁移既有短窗口数据的口径。
