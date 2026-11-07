# 任务清单

## 1. 规格

- [x] 1.1 `specs/data-census/spec.md` 两条要求：census 尺寸计数容纳 32 位以上量级；单店探测失败不得毁掉整轮 census
- [ ] 1.2 `openspec validate fix-census-refresh --strict` 通过

## 2. 实现

- [x] 2.1 `models.py`：`DataCensus.approx_rows` / `total_size_bytes` 改 `BigInteger`
- [x] 2.2 新增 alembic 版本 `0017_census_bigint`（down_revision=`0016_policy_launcher`），两列 `ALTER COLUMN TYPE bigint`，非 PG 方言直接 return
- [x] 2.3 `visibility/census.py`：本地腿、`_shard_servers`、每个 shard probe 各自 `session.begin_nested()` SAVEPOINT，单腿失败只回滚自己

## 3. 测试

- [x] 3.1 单测：模型两列为 `BigInteger`（防回归到 `Integer`）
- [x] 3.2 单测：写入 `total_size_bytes=2_873_810_944` 与 `approx_rows=3e9` 的 census 行可读回原值
- [x] 3.3 单测：一脚一条 SAVEPOINT（本地 + 店列表 + 2 个 shard = 4 条）、三行 census 全落库
- [x] 3.4 生产库实测：`total_size_bytes` 越过 int4 上限后，第二轮报错换成 `InFailedSqlTransaction`（证明 int4 已修、露出下一个缺陷）；`ALTER` 后列类型为 `bigint`，`alembic_version` 已标 `0017_census_bigint`
- [ ] 3.5 生产实测：0.5.44 镜像的 reconciler tick 后 `data_census.sampled_at` 前进、本地行 `total_size_bytes≈2.87e9` 落库、`shard_xinru3` 行带错误文本而其余店正常

## 4. 发布

- [ ] 4.1 版本号 + CHANGELOG + PyPI（patch bump）
- [ ] 4.2 镜像重建（scraw `FD_ODM_INSTALL` 下限跟随）+ 两 CronJob 滚动
- [x] 4.3 生产库列类型放宽（`ALTER COLUMN TYPE bigint`，未重写表）

## 5. 收尾

- [ ] 5.1 `openspec archive fix-census-refresh`
