# 读面覆盖与降级声明 — 接入清单（change: define-ingest-revision-and-coverage，任务 2.4）

> 性质：**盘点，不是承诺**。本表只回答"哪些读面返回观测值、它们现在自证到什么程度"。
> 行号由 AST 取函数跨度得出（`node.lineno`..`node.end_lineno`），可复算；`server.py` 增删行后请重算，别手改。
> 未接入的读面不做批量改：改一个热路径读面（`read_series`）会改变它的成本与返回体，
> 属独立决定，见文末「缺口与代价」。

## 判定口径

R8 只约束**返回观测值**的读面。目录类、实体类、元数据类读面不涉及"这份数据完整吗"，
记为「不适用」，不计入待补。

| 状态 | 含义 |
|---|---|
| 已声明 | 返回体能说明覆盖范围与降级（`gaps` / `missing` / `degraded` / 显式 note） |
| 部分 | 只覆盖了一种缺口形态（如只报"落不了历法的日期"），区间缺口不可见 |
| 不适用 | 不返回观测值 |
| 待接入 | 返回观测值但不自带任何覆盖/降级信息 |

## 清单

| 读面 | 位置 | 返回观测值 | 状态 | 证据 |
|---|---|---|---|---|
| `read_series`（`fill_gaps=true`） | `server.py:1539-1569`（外层 1442-1632） | 是 | 已声明 | 返回 `gaps` / `fill_attempted` / `fill_failed` / `fingerprint`，并在 `note` 里写明"结果不完整" |
| `read_series`（默认路径） | `server.py:1571-1630`（外层 1442-1632） | 是 | **部分** | 只报"落不了历法的日期"进 `gaps`（`excluded`），**区间缺口不可见**；空窗口靠 `note` 文字说明，无结构化字段 |
| `read_series`（registry 路由） | `server.py:1495-1525`（在 1442-1632 内） | 是 | 待核实 | 走联邦通道，返回形状自称同形，但未验证是否带覆盖声明 |
| `read`（按日期集合） | `server.py:1341-1435` | 是 | 不适用 | 入参是 `dates[]` 而非区间；逐点缺失即逐点返回，无"区间缺口"可声明 |
| `fetch`（强制刷新单点） | `server.py:1636-1644` | 是（单点） | 不适用 | 写路径，非区间读 |
| `flower_price_series` | `flower_tools.py:459+` | 是 | 部分 | 有 `limit` 截断语义，未见区间覆盖声明 |
| `restaurant_series` | `restaurant_tools.py:859+` | 是 | 部分 | 同上；系列按 code 读取，窗口未声明 |
| `restaurant_company_financials` / `flower_company_financials` | 垂直工具 | 是 | 待核实 | 财报是快照序列，需确认是否带期间覆盖 |
| `flower_planted_area` / `flower_trade` | 垂直工具 | 是 | 待核实 | 同上 |
| `coverage_report` | `coverage_tools.py` | 否（报采集覆盖） | 不适用 | 它回答"抓过没抓过"，不回答"这次读到的完整吗"；两者不可互相替代 |
| 其余 26 个只读工具（catalog / entity / graph / scope / policy） | 各 `*_tools.py` | 否 | 不适用 | 目录与元数据读面 |

## 已完成的结构性工作（本轮）

- `dispatch._read_range_one_ex()` 把所有读路径的内部来源标注为 `cache` / `fetch` /
  `cache_degraded` / `none`，并在失败时记录来源名。**"取不到"与"没有"从这一层起不再同形。**
- `dispatch.read_range_with_coverage()` 提供带 `coverage{requested,covered,missing}` 与
  `degraded{degraded,reason,sources_failed}` 的读入口，复用 `fetch.coverage.coverage()`
  （与补数同一口径），不改 `read_range` 行为。

## 缺口与代价（未做，需决定）

**`read_series` 默认路径是唯一一个"高频读 + 无区间覆盖声明"的面。**

要给它补 `coverage`，需要序列的**全量日期**（`coverage()` 的 `all_dates` 基准），
即每次读都多一次 `_read_base_range(0000-01-01 … 9999-12-31)` 的 DISTINCT 查询。
这条读面是产品最热的路径，代价必须被量过再决定：

1. **方案 A**：默认加 `coverage`，接受一次额外查询（简单，可能拖慢热路径）。
2. **方案 B**：加 `declare_coverage=true` 开关（零成本，但默认不给声明——与 R8 的"必须声明"冲突）。
3. **方案 C**：把全量日期做成物化（`concept_series_calendar` 表），读时 O(1)。

我倾向 **C**，因为它同时解决 `read_series` 与未来所有读面，且 `series_dates()` 现在每次
补数都在重算同一件事。但这是性能与存储取舍，不在本 change 里擅自定。

## 未核实项

`restaurant_company_financials` / `flower_company_financials` / `flower_planted_area` /
`flower_trade` / registry 路由读面：需要逐个看返回体才能定档。列为"待核实"而不是猜一个状态。
