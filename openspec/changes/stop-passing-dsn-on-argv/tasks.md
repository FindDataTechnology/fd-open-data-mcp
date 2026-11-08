# 任务清单

## 1. 规格

- [ ] 1.1 `openspec validate stop-passing-dsn-on-argv --strict` 通过

## 2. 实现

- [ ] 2.1 `reconciler._direct_manifests()`：命令里去掉 `--db-url "$FD_OPEN_DATA_MCP_DATABASE_URL"`
- [ ] 2.2 `bulk_ingest_astock_daily.py`：`--db-url` 缺省时读 `FD_OPEN_DATA_MCP_DATABASE_URL`，两者皆无则 `ap.error` 点明
- [ ] 2.3 同批检查其它 direct 脚本（`bulk_ingest_stock_indicators_events.py` 等）是否有同样形状

## 3. 测试

- [ ] 3.1 单测：`_direct_manifests` 生成的 Job 命令不含 DSN、不含 `--db-url`
- [ ] 3.2 单测：脚本无 flag 时从 env 取 DSN；两者皆无时报错退出

## 4. 发布与验证

- [ ] 4.1 0.5.48 出片 + 两 CronJob 滚动 + `clusters.image`
- [ ] 4.2 真集群验证：新建的 direct Job 的 `spec.containers[0].command` 与 pod 内 `/proc/*/cmdline` 都不含口令；脚本仍能连库

## 5. 收尾

- [ ] 5.1 台账记一条
- [ ] 5.2 `openspec archive stop-passing-dsn-on-argv`
