# 任务清单

## 1. 规格

- [x] 1.1 `openspec validate add-astock-per-symbol-window --strict` 通过

## 2. 实现

- [x] 2.1 `per_symbol_start()`：`max(global_start, sym_max+1)`，再按 `--max-catchup-days` 夹到 `end - cap`；`--start` 显式时直接返回它
- [x] 2.2 `stored_anchors` 之外新增 per-symbol 最新日期查询（同一 index-bound 分块形状，绝不整表扫）
- [x] 2.3 `ingest_symbol` 接受 per-symbol start；主循环统计 caught_up / capped 并在日志里点名

## 3. 测试

- [x] 3.1 单测：落后但未超 cap → 从自身最后日+1 起
- [x] 3.2 单测：超 cap → 夹到 cap 边界并标记 capped
- [x] 3.3 单测：已最新 → 用全局窗口；显式 `--start` → 全部按它
- [x] 3.4 真库实测（`--dry-run`，不写库）：`000016,000001` → 日志 `window: 1 current, 0 caught up, 1 capped at 30d`，点名 `000016:2026-09-03->2026-10-10`（识别到自身停在 09-03、按 cap 边界起），`000001` 保持 current；`--max-catchup-days 0` 回到单窗口；显式 `--start` 时日志标 `pinned by --start`

## 4. 发布

- [x] 4.1 已更新 `fd-scripts` ConfigMap（`per_symbol_start` 与 `--max-catchup-days` 在活册中确认），无需出镜像
- [ ] 4.2 观察一次调度 run：caught_up 计数与行数符合预期

## 5. 收尾

- [ ] 5.1 台账记一条
- [ ] 5.2 `openspec archive add-astock-per-symbol-window`
