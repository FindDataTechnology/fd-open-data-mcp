# 任务清单

## 1. 规格

- [ ] 1.1 `openspec validate fix-financials-code-map-entity-join --strict` 通过

## 2. 实现

- [ ] 2.1 `bulk_ingest_financials_aggregate.py::load_code_map`：JOIN `entities`，要求 `e.entity_type='stock'`；同码冲突记日志并保留首个
- [ ] 2.2 `bulk_ingest_stock_financials.py`：同样的 JOIN + 冲突日志
- [ ] 2.3 全仓 grep 其它"只看 esi.entity_type 建映射"的写法，一并收口

## 3. 测试

- [ ] 3.1 单测：行业实体上的 stock 标识**不进**映射
- [ ] 3.2 单测：同码两实体 → 保留首个并告警

## 4. 部署与验证

- [ ] 4.1 更新 `fd-scripts` ConfigMap 两脚本（无需出镜像）
- [ ] 4.2 真库 dry-run：确认 `000712` 不再映射到 2053

## 5. 收尾

- [ ] 5.1 台账记一条（含"标识表的 entity_type 是声明、不是事实"这一教训）
- [ ] 5.2 `openspec archive fix-financials-code-map-entity-join`
