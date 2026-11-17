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

- [x] 4.1 出片 + 滚版后核对线上 KPI 从 45.5% 变为 62.5%（同一窗口、同一数据）。
  验证：`kubectl -n fd-prod exec deploy/fd-open-data-mcp -- python -c "...kpi_snapshot..."`。
  **已完成（2026-11-12）**：
  - 镜像 `sha-b264435` 由 run 43162173596 铸造（digest 变了才铸：
    `NEW sha256:f907ac0b… / OLD sha256:7bfb8de7…`）。上产前用短命 pod 实测**可拉**
    （`Running`，不是 `ImagePullBackOff`）——ccr 靠 cheap-3 的 tcr-relay 回灌，首次
    直拉会 NotFound；不手动补 relay（抢 flock 会得到假成功）。
  - 清单 `fd-infra-deploy@2960bbe`：init `migrate-schema` 与主容器**两处**同滚到
    `sha-b264435`（只改一处会让 init 跑旧片）。Argo `all-services-prod` 同步到
    `2960bbe`，`deploy/fd-open-data-mcp` 滚动完成。
  - 新 pod `fd-open-data-mcp-7bf9b45476-q5t28`：`init(migrate-schema)=Completed(0)`、
    主容器 ready、restarts=0 —— 纯读侧改动，库头未动，无 CrashLoop。
  - 同窗口实测（pod 内）：`24h 分布 {'failed': 3, 'no_op': 3, 'success': 5}` →
    `kpi_snapshot {'running': 0, 'success_rate': 62, 'failures': 3, 'no_signal': 3,
    'unknown': 0}`。改动前同一函数给 45。
  - 页面实证（走发布入口，不只看函数）：`http://100.64.0.12:31851/panel` →
    `62% | 24h 成功率（可判定）`、`3 | 24h 失败 · 无信号 3`。
    ⚠ 记一个坑：`https://mcp.finddatatech.cloud/panel` 返回的是**网关 SPA**
    （889 字节，`AI Gateway & Registry`）而不是本面板；本面板入口是 Service
    `fd-open-data-mcp` 的 NodePort **31851**（容器端口 8300）。
