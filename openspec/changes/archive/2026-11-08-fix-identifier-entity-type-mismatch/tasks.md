# 任务清单

## 1. 规格

- [x] 1.1 `openspec validate fix-identifier-entity-type-mismatch --strict` 通过

## 2. 执行

- [x] 2.1 快照 `/tmp/idfix-snapshot.tsv`（0600，2,636 行，含 id/entity_id/type/source/identifier）
- [x] 2.2 删除 stock-on-person 标识 **1,793** 行（独立事务）
- [x] 2.3 stock-on-fund 843 行：**561 行改型为 fund**（目标键空闲）+ **282 行删除**（同 (entity,source) 已有 fund 行，唯一约束 `uq_entity_source` 会撞，实测 `(fund, 9877, akshare) already exists`）
- [x] 2.4 复核：不一致只剩延后类（organization 240 / country→city 36 / industry 32 / index 24 / exchange 3）；**astock 家族实体仍 5,203**；`stock` 标识挂 person 的计数 **0**；`512660/510810/562550/512660.SS` 已**解析不到任何人**；fund 标识 503→1,064；标识总数 88,182→86,107

## 3. 收尾

- [ ] 3.1 台账记一条（含"上游 fund_manager 摄取应停止写 stock 标识"的挂账）
- [ ] 3.2 `openspec archive fix-identifier-entity-type-mismatch`
