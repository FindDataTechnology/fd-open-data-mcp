## 1. 口径与单一真源

- [x] 1.1 `visibility/scan.py`：`ALERT_STATUSES` 公开 + 新增
  `NO_SIGNAL_STATUSES = ("no_op", "redundant")`，`_ALERT_STATUSES` 留别名。
  验证：`from fd_open_data_mcp.visibility.scan import ALERT_STATUSES` 可导入。
- [x] 1.2 `panel/app.py::kpi_snapshot`：分母改 `success + failed + zero_yield`；
  返回新增 `no_signal`、`unknown`；docstring 写明口径与线上实测数字。
  验证：见 2.1–2.3。

## 2. 用例（含负例，证明门会红）

- [x] 2.1 `test_success_rate_excludes_no_signal_runs`：1 success + 1 no_op + 1
  redundant → 100%（旧口径为 50%）。**负例实测**：把 `NO_SIGNAL_STATUSES`
  注入分母后此条唯一变红（1 failed / 5 passed），还原后 6 passed。
- [x] 2.2 `test_success_rate_counts_failure_and_surfaces_unknown`：真失败进分母
  （1 success + 1 failed + 1 `partial` → 50%），且 `unknown == 1`。
- [x] 2.3 `test_kpi_snapshot_empty_db` 与窗口用例补齐 `no_signal` / `unknown` 断言。

## 3. 显示

- [x] 3.1 `home.html`：「24h 成功率（可判定）」；失败格标签后追加
  `无信号 N` / `未知状态 N`（为 0 时不显示）。
- [x] 3.2 `i18n_en_shell.py`：新增三条 EN 译文（miss 会回退中文原文，不会崩）。

## 4. 落地

- [ ] 4.1 出片 + 滚版后核对线上 KPI 从 45.5% 变为 62.5%（同一窗口、同一数据）。
  验证：`kubectl -n fd-prod exec deploy/fd-open-data-mcp -- python -c "...kpi_snapshot..."`。
