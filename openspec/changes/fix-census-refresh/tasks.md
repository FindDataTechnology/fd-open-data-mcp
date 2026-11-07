# 任务清单

## 1. 规格

- [x] 1.1 `specs/data-census/spec.md` 两条要求：census 尺寸计数容纳 32 位以上量级；单店探测失败不得毁掉整轮 census
- [ ] 1.2 `openspec validate fix-census-refresh --strict` 通过

## 2. 实现

- [x] 2.1 `models.py`：`DataCensus.approx_rows` / `total_size_bytes` 改 `BigInteger`
- [x] 2.2 新增 alembic 版本 `0017_census_bigint`（down_revision=`0016_policy_launcher`），两列 `ALTER COLUMN TYPE bigint`，非 PG 方言直接 return
- [x] 2.3 `visibility/census.py`：本地腿、`_shard_servers`、每个 shard probe 各自 `session.begin_nested()` SAVEPOINT，单腿失败只回滚自己
- [x] 2.4 `visibility/census.py`：连接改在 `begin_nested()` **内部**取（0.5.45）。0.5.44 在循环前取一次 `conn = session.connection()`，SQLAlchemy 2.0 因此静默不发 `SAVEPOINT`，隔离形同虚设；顺序约束写进 `refresh_census` 注释
- [x] 2.5 `_obs_month_distribution` 改收 session、自带 SAVEPOINT 并独立成腿（原先紧跟本地腿，本地腿一失败分布一起丢；SQLite 无 `pg_total_relation_size` 时即触发）

## 3. 测试

- [x] 3.1 单测：模型两列为 `BigInteger`（防回归到 `Integer`）
- [x] 3.2 单测：写入 `total_size_bytes=2_873_810_944` 与 `approx_rows=3e9` 的 census 行可读回原值
- [x] 3.3 单测：一脚一条 SAVEPOINT（本地 + 分布 + 店列表 + 2 个 shard = 5 条）、三行 census 全落库
- [x] 3.4 生产库实测：`total_size_bytes` 越过 int4 上限后，第二轮报错换成 `InFailedSqlTransaction`（证明 int4 已修、露出下一个缺陷）；`ALTER` 后列类型为 `bigint`，`alembic_version` 已标 `0017_census_bigint`
- [x] 3.5 单测：`before_cursor_execute` 断言 SQL 里**真出现 `SAVEPOINT`**。只断言 `begin_nested()` 被调过拦不住静默降级——0.5.44 就是那样过的单测
- [x] 3.6 真 PG 演练（生产库，pod 内把修好的 `census.py` 覆盖进 site-packages）：给 `shard_xinru2` 注入 `UndefinedTable` → 该店行记 `probe failed: (psycopg2.errors.UndefinedTable) …`、`gz_master`=`7142420`/`2874908672`、`shard_xinru3`=`46440544` 均落库、`sampled_at` 前进到 `21:07:39`、共 6 条 SAVEPOINT 语句；随后一次干净刷新把 `shard_xinru2` 行恢复（`97878069`，error 清空）
- [ ] 3.7 生产实测：0.5.45 镜像的 reconciler tick 后 `data_census.sampled_at` 继续前进（镜像身份以 `fd_open_data_mcp.__version__` 打印为准，不看 pod 里跑了多久）

## 4. 发布

- [x] 4.1 版本号 + CHANGELOG + PyPI：0.5.44（int4 + 首版 SAVEPOINT）、0.5.45（SAVEPOINT 真生效）
- [x] 4.2 镜像重建（scraw `FD_ODM_INSTALL` 下限跟随）+ 两 CronJob 滚动：0.5.44 → `sha-7e97115`
- [ ] 4.3 0.5.45 镜像重建（`sha-<新>`）+ TCR relay + `crawl-reconciler` / `coverage-expander` / `clusters.image` 三处滚动
- [x] 4.4 生产库列类型放宽（`ALTER COLUMN TYPE bigint`，未重写表）

## 5. 收尾

- [ ] 5.1 `openspec archive fix-census-refresh`
