# 任务清单

## 1. 规格

- [x] 1.1 `openspec validate add-astock-new-series-flag --strict` 通过

## 2. 实现

- [x] 2.1 `_astock_ident(..., allow_new=False)`：显式代码 + `allow_new` 时，把「无行」候选保留（每个 bare code 只取一个实体，bare 标识优先于带后缀孪生），并在日志里点名
- [x] 2.2 `resolve_entities(..., allow_new=False)` 透传；`main` 增加 `--allow-new-series`，`--universe` 永远走存在性过滤
- [x] 2.3 `--dry-run` 同样列出将新建序列的代码

## 3. 测试

- [x] 3.1 单测：无 `--allow-new-series` 时保持现行为（无行代码被过滤）
- [x] 3.2 单测：带 `allow_new` 时无行代码被保留、同一 code 只保留一个实体、有行孪生仍优先
- [x] 3.3 单测：`--universe` 带 `allow_new` 也不扩面
- [x] 3.4 真库实测：`001206`（原先零行）`--dry-run` 报 110 行、真跑写入 110 行，分片读回 `eid=5295 -> (110, '2026-10-08', '2026-11-06')`——qfq 兜底路径（锚点 0 个）按设计生效

## 4. 发布与扩面

- [x] 4.1 更新 `fd-master/fd-scripts` ConfigMap 三键：`bulk_ingest_astock_daily.py`（带 `--allow-new-series`）、`run_astock_backfill_batches.py`（透传该开关）、`new-series-symbols.txt`（626 票）；无需出镜像，reconciler/Job 启动时读 ConfigMap
- [x] 4.2 生成无序列代码清单并按单写者 50 票/批 + 30s 冷却 + 检查点跑完。**口径纠正**：标识表里 6,401 个六位代码中无序列者 1,825，但其中 1,085 个是 `51/56/58/52/53/15` 开头的 **ETF/基金/债券**、327 个是 `92` 开头的 **北交所**——都被错标成 `entity_type='stock'`；与家族同口径（`00/30/60/68`）的真实 A 股缺口只有 **626** 个，本轮只扩这 626
- [x] 4.3 验收（626 票，集群常驻 Pod `zz-astock-newseries`）：**13/13 批 `failed_legs=0`、runner exit=0**；626/626 有行（0 空），其中 **613 票 79 天全齐**，余下 44–78 天为新上市/停牌（`000004` 仅 1 天，源站同样只有 1 天）；家族窗口内有行实体 **4,577 → 5,203**；抽样逐日对源站 `001217` 79=79、`301234` 72=72、`688799` 79=79、`000004` 1=1，**双向差集全空**

## 5. 收尾

- [ ] 5.1 台账 §15.138 记一条（扩面口径与结果）
- [ ] 5.2 `openspec archive add-astock-new-series-flag`
