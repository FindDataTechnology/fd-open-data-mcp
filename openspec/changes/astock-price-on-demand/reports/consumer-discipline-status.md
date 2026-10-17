# 消费方纪律现状（task 8.3）

> 本文件记录**事实与缺口**，不是实现。任务明文要求"若 fpt 侧尚未实现该纪律，本项只记录现状并在报告中点名，不擅自改 fpt"。

## 1. 本变更提供了什么信号

`read_series(fill_gaps=True)` 与 `fill_gaps()` 返回：

| 字段 | 含义 |
|---|---|
| `gaps` | 该窗口仍缺失的区间列表（含已探明无数据的休市段） |
| `fill_attempted` | 是否尝试过补数 |
| `fill_failed` | 补数是否失败（上游不可达等，区别于"确实没数据"） |
| `fingerprint` | 返回内容的内容哈希，供调用方钉住"当时那份数据" |

## 2. fpt 侧现状（2026-10-17 实测）

`fd-paper-trading` 的回测取数只做**一种**保护：

```
src/fd_paper_trading/api/routes/strategies.py:281
    if not bars:
        raise EvaluationError(f"no bar data for symbol ... in {start}..{end}")

src/fd_paper_trading/api/routes/strategies.py:309  (权重轨)
    if not any(bars_by_symbol.values()):
        raise EvaluationError(f"no bar data for universe ... in {start}..{end}")
```

即：**完全无数据时拒绝**，这与既有 spec 一致。

**缺口（如实点名）**：没有任何**部分覆盖**检查。若某标的在 730 天窗口里只有 12 天数据，回测会把那 12 天当成整个窗口算出一个收益率，结果看起来像真的。

`FdOpenDataMarketDataProvider.get_daily_bars` 走 `dispatch.read_range`，该函数**默认不补数、也不报告缺口**（设计如此：`read_series` 的默认路径契约是"不联网"）。因此 fpt 今天既拿不到 `gaps`，也不会主动要。

## 3. 后果量化

- **已有 600519 之外的新标的**：首次回测会因零数据被拒绝（错误清晰，可接受）。
- **部分覆盖的标的**（例如早期逐日读留下的碎数据）：**静默算出偏短的收益率**。这是当前真实的 silent-wrong-data 风险。
- 中央库里的 `price.close`（3,533 只、2026-06-09→09-18、每只中位数 44 行）正是"部分覆盖"的典型形态，一旦被 fpt 读到就会触发该风险。

## 4. 建议的消费方纪律（未实施，交由 fpt 仓自己的 change 决定）

回测在取数后应当：

1. 请求时带 `fill_gaps=true`（或至少能拿到覆盖度信息）；
2. 若 `gaps` 非空，**拒绝运行**并指名缺口，或至少把 `gaps` 与 `fingerprint` 写入 run 记录，使"这个收益率是在 12 天数据上算的"事后可查；
3. 把 `fingerprint` 存进 `strategy_runs.params`，使两次不可比的 run 事后能被发现（前复权序列会被上游重写）。

## 5. 为什么这次不改 fpt

- 任务明确划定范围；
- 消费方纪律是 fpt 自己的 spec 变更（要改 `strategy-backtest` 的要求与既有测试），属于独立 change；
- 本变更已把信号准备齐备（`gaps` / `fill_failed` / `fingerprint`），fpt 侧接入是纯消费工作。

**待办**：在 `fd-paper-trading` 立一个 change（建议名 `backtest-data-coverage-gate`），把 §4 的三条落成 spec 要求。
