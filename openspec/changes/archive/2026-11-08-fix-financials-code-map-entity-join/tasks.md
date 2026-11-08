# 任务清单

## 1. 规格

- [ ] 1.1 `openspec validate fix-financials-code-map-entity-join --strict` 通过

## 2. 实现

- [ ] 2.1 `bulk_ingest_financials_aggregate.py::load_code_map`：JOIN `entities`，要求 `e.entity_type='stock'`；同码冲突记日志并保留首个
- [ ] 2.2 `bulk_ingest_stock_financials.py`：同样的 JOIN + 冲突日志
- [ ] 2.3 全仓 grep 其它"只看 esi.entity_type 建映射"的写法，一并收口

## 3. 测试

- [x] 3.1 守卫测试：扫描五个 ingest，凡 `FROM entity_source_identifiers` 后无 `JOIN entities` 即失败（生产语义由 9,544 保留 / 16 剔除实证）
- [x] 3.2 聚合 ingest 改为保留首个并计数告警（`N code collisions kept-first`）；真库当前无同码冲突（0），逻辑由代码审阅覆盖

## 4. 部署与验证

- [x] 4.1 已更新 `fd-scripts` ConfigMap **五个**脚本（聚合/个股财报/astock/基金净值/指标事件），均验证 `JOIN entities` 在活册中
- [x] 4.2 真库实证：旧口径 `000712` 命中 2053（行业），新口径下 `000712 → 3125`（锦龙股份）；新预言机保留 9,544 行、剔除 16 行

## 5. 收尾

- [x] 5.1 台账 §15.138 续二十五（含三条纪律 + 两次自查推翻重归位的完整过程）
- [ ] 5.2 `openspec archive fix-financials-code-map-entity-join`
