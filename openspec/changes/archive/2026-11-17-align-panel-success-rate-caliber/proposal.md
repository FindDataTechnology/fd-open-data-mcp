## Why

面板首页那颗「24h 成功率」在线上报 **45.5%**，读数指向"近一半运行失败"，但同一时刻
`policy_runs` 的 24h 终态分布是 `failed 3 / no_op 3 / success 5`（running=0）——
真正该报警的失败只有 3 条，可判定成功率是 5/8 = **62.5%**。

根因是分母口径：`kpi_snapshot` 把**除 running 之外的所有终态**当作分母，于是
`no_op`（空计划：这一轮没有要做的）也成了"失败"。而告警分类器
（`visibility/scan.py::_classify`）早就把 `no_op` 明确判为**不可告警**：
「`no_op` (empty plan) and `redundant` (fetched, nothing new) are recorded but
non-alerting」。同一个"什么算失败"在仓里有两份定义，面板这份比告警那份更悲观。

记录中的原假设是"`status='running'` 被算成失败"，实测**证伪**：`running` 早在
2026-09-15 的 `8c60c2b` 就从分子分母里排除了（`status != "running"`）。真正压坏
读数的是 `no_op`。这条记录一并改正，免得下一个人再去查一个不存在的问题。

## What Changes

- **成功率口径对齐告警分类器**：分母 = `success + failed + zero_yield`，即
  `visibility.scan.ALERT_STATUSES` 所定义的可判定终态；`no_op` / `redundant`
  记为"无信号"，进 `no_signal` 计数但**不进分母**。
- **判据单一真源**：把告警用的状态集合升为公开常量 `ALERT_STATUSES`，并补
  `NO_SIGNAL_STATUSES`；面板直接 import，不再自己写一份 `("failed","zero_yield")`。
- **不隐藏任何一行**：KPI 里新增 `no_signal` 与 `unknown` 计数并显示在"24h 失败"
  那一格的标签后面。未登记的新状态（如 `partial`）不进分母但必须可见——否则
  新状态一出现就从读数里静默消失。
- **标签说清口径**：「24h 成功率」→「24h 成功率（可判定）」，中英双语。

## Impact

- `fd_open_data_mcp/panel/app.py::kpi_snapshot` — 分母与返回字段
  （新增 `no_signal` / `unknown`）。
- `fd_open_data_mcp/visibility/scan.py` — `ALERT_STATUSES` / `NO_SIGNAL_STATUSES`
  公开化，`_ALERT_STATUSES` 保留别名兼容。
- `fd_open_data_mcp/panel/templates/home.html`、`panel/i18n_en_shell.py` — 标签与计数。
- `tests/test_panel_home_cockpit.py` — 3 条新用例（含负例：把 `no_op` 计回分母会红）。
- 无 schema 变更、无数据回填；线上读数会**变大**（同样的运行，不再把健康算成故障）。
