# 任务清单

## 1. 规格

- [x] 1.1 `openspec validate stop-passing-dsn-on-argv --strict` 通过

## 2. 实现

- [x] 2.1 `reconciler._direct_manifests()`：命令里去掉 `--db-url "$FD_OPEN_DATA_MCP_DATABASE_URL"`
- [x] 2.2 `bulk_ingest_astock_daily.py`：`--db-url` 缺省时读 `FD_OPEN_DATA_MCP_DATABASE_URL`，两者皆无则 `ap.error` 点明
- [x] 2.3 其它 direct 脚本无需改：`--db-url` 不再被 launcher 拼入，缺省即走 env；`bulk_ingest_stock_indicators_events.py` 等仍显式接受该 flag（本地可用）

## 3. 测试

- [x] 3.1 单测：`_direct_manifests` 生成的 Job 命令不含 DSN、不含 `--db-url`
- [x] 3.2 单测：脚本无 flag 时从 env 取 DSN；两者皆无时报错退出

## 4. 发布与验证

- [x] 4.1 0.5.48 出片（镜像 `sha-7632f17`，TCR relay 14:42:41 回灌 ccr）+ 两 CronJob 滚动 + `clusters.image` id9 + 清单对齐
- [x] 4.2 真镜像验证（0.5.48 pod 内跑 launcher 的 `_direct_manifests`）：命令 `… bulk_ingest_astock_daily.py --universe --slice-all 8`，`--db-url`=False、`postgresql`=False、`FD_OPEN_DATA_MCP_DATABASE_URL` 在 env=True

## 5. 收尾

- [ ] 5.1 台账记一条
- [ ] 5.2 `openspec archive stop-passing-dsn-on-argv`
