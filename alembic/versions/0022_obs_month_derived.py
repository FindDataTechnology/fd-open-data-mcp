r"""obs_month 改由库派生：任何写者都不再能留下 NULL（触发器 + 回填）。

问题
----
0013 建了 ``obs_month``（``date`` 的派生月桶）并在迁移里回填过一次，但它**只约束了
迁移本身，没约束写者**。``fetch.cache`` 的三条写路径每条都算 ``obs_month_of()``，
而 ``scripts/bulk_ingest_*.py`` 用 ``execute_values`` 直写、列清单里没有 ``obs_month``，
``scripts/financial_consumer.py`` 的单行 INSERT 也没有。

实测 2026-11-12 01:20（生产库）：

* 近 24h ``akshare`` 写入 1,896 行，``obs_month`` 填充率 **0%**；
* 全库 945,379 行里 22,214 行（2.35%）为 NULL，集中在
  ``tenure_days`` / ``price`` / ``aum_total`` / ``best_return`` / ``nav`` / ``yield``
  ——正好是走批量直写的那几族。

后果不是数据丢失（读侧 ``coalesce(obs_month, date)`` 能兜），而是两处静默失真：

1. ``visibility/census.py`` 把这些行归进 NULL 桶，注释把该桶定义成
   "sentinels/garbage（``obs_month_of`` 映射不到的）"——于是**正常日期行被报成哨兵垃圾**，
   容量报表据此低估可映射行数；
2. 月频/时频的覆盖去重按 ``obs_month`` 分桶（日频故意不用，见 ``coverage.py`` 的
   "bucketing day rows on obs_month gutted fund-aum ~91%"），NULL 行只能退回原始日期，
   同一张表里并存两套覆盖口径。

为什么用触发器，而不是去改那五支脚本
------------------------------------
爬虫集群镜像（``clusters.image``）从 PyPI 装 ``fd-open-data-mcp``，**看不到本仓脚本的
当前版本**；``retention_bulk.py`` 的模块注释已经把这件事写清楚了——"留存语义只在写入
进程所用镜像里生效"。而"第五支脚本一定会漏"在本仓被验证过不止一次（``tests/
test_bulk_retention_guard.py`` 就是为它写的静态闸）。

``obs_month`` 是 **100% 派生**的（不携带任何来源信息，不等于"写入时算的"），所以
库内派生与脚本内派生语义等价，而库内派生能覆盖**包括旧镜像在内的一切写者**，
也不需要在出片链上再滚一次爬虫镜像。

触发器为什么是 ``BEFORE INSERT OR UPDATE``（不带 ``OF 列`` 限定）
---------------------------------------------------------------
``ON CONFLICT ... DO UPDATE SET value=..., fetched_at=...`` 只更新 SET 列表里的列；
列限定触发器（``UPDATE OF obs_month``）在这种 upsert 的更新分支上**不会触发**，
而库里现存的 NULL 恰恰主要是这么留下来的。不带列限定则每次写入都重算——
反正它是派生的，任何显式传入的值都该被覆盖。

回填只动"现值与派生值不一致"的行（含 NULL），不重写已经正确的 92 万行，避免给这张
生产热表制造无谓的膨胀。

Revision ID: 0022_obs_month_derived
Revises: 0021_retention_exemption
"""

from alembic import op

revision = "0022_obs_month_derived"
down_revision = "0021_retention_exemption"
branch_labels = None
depends_on = None

# Mirrors fd_open_data_mcp.semantic.dates.obs_month_of 与 0013 的 SQL CASE —— 三处同源。
# 0013 的回填 SQL 是内联子查询；这里抽成不可变函数，好让触发器与回填共用同一份定义
# （两份 CASE 各自漂移过一次，正是本次要消灭的形态）。
_MONTH_OF_FN = r"""
CREATE OR REPLACE FUNCTION fd_obs_month_of(d text) RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $fn$
  SELECT CASE
    WHEN d IS NULL THEN NULL
    WHEN d ~ '^\d{4}-W\d{2}$' THEN
      to_char(to_date(d || '-1', 'IYYY-"W"IW-ID'), 'YYYY-MM')
    WHEN d ~ '^\d{4}$' THEN d || '-12'
    WHEN d ~ '^\d{4}-\d{2}-\d{2}T' THEN substr(d, 1, 7)
    WHEN d ~ '^\d{4}-\d{2}(-\d{2})?.*$' THEN substr(d, 1, 7)
    ELSE NULL
  END
$fn$;
"""

_TRIGGER_FN = r"""
CREATE OR REPLACE FUNCTION fd_sem_obs_month() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN
  NEW.obs_month := fd_obs_month_of(NEW.date);
  RETURN NEW;
END
$fn$;
"""

_BACKFILL_SQL = """
UPDATE semantic_observations SET obs_month = fd_obs_month_of(date)
 WHERE obs_month IS DISTINCT FROM fd_obs_month_of(date)
"""


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite 测试消费者按 models 建表，没有这个列语义（0013 同款取舍）。
        return
    import sqlalchemy as sa

    bind.execute(sa.text(_MONTH_OF_FN))
    # 回填放在建触发器**之前**：触发器本身就是"写入时重算一遍"，先建它会让回填
    # 每行多算一次函数（94.5 万行 × 3 次 vs × 2 次），没有任何收益。
    #
    # 不按 id 区间分批（0013 那么做了）：那张表当时是 650 万行、id 连续；这里 id 跨
    # 3.14 亿而只有 94.5 万行，按 5 万切要扫 6,284 次空区间，比一次全表扫贵得多。
    bind.execute(sa.text(_BACKFILL_SQL))

    bind.execute(sa.text(_TRIGGER_FN))
    bind.execute(sa.text(
        "DROP TRIGGER IF EXISTS trg_sem_obs_month ON semantic_observations"))
    bind.execute(sa.text(
        "CREATE TRIGGER trg_sem_obs_month BEFORE INSERT OR UPDATE "
        "ON semantic_observations FOR EACH ROW "
        "EXECUTE FUNCTION fd_sem_obs_month()"))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    import sqlalchemy as sa

    # 只撤守卫：触发器删掉后写者又能留 NULL；**已派生的值保留**——它们是正确数据，
    # 抹掉反而制造不一致。真正的回退顺序是先确认没有旧镜像在写，再来撤这里。
    bind.execute(sa.text(
        "DROP TRIGGER IF EXISTS trg_sem_obs_month ON semantic_observations"))
    bind.execute(sa.text("DROP FUNCTION IF EXISTS fd_sem_obs_month()"))
    bind.execute(sa.text("DROP FUNCTION IF EXISTS fd_obs_month_of(text)"))
