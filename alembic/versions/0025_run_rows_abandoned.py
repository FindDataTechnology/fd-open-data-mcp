r"""run_rows_abandoned — 运行行记「这一轮放弃了多少格」。

为什么加这一列
--------------
`bound-unfetchable-crawl-cells` 让执行器对「同 (concept, entity) 连续失败 K 格」止损，
并把结果打进 pod 日志（`ABANDONED N cell(s) across M pair(s)`，2026-11-16 真机冒烟：
45 个请求 / 3 个实体 / 每实体只真打 2 次上游）。但**闸看不见它**：
`check_policy_freshness` 只读 `plan_cells` / `rows_attempted` / `rows_new`。

后果不是纸面的：一个**完全被放弃**的轮次（`rows_attempted=0`、`rows_new=0`、Job 成功）
会被 D3 分类表读成 `zero_yield` —— 「取到了但没落行」，而事实是「一个上游请求都没发」。
两者的处置方向相反（一个查抽取/列映射，一个查这批实体到底谁有数据），混在一起只能猜。

口径
----
* **可空**，且缺失表示「这一轮的执行器没报」，与 `rows_*` 同一约定 ——
  绝不能拿 `NULL` 读成 `0 abandoned`；
* 由执行器在 close 时按 `SCRAW_JOB_REF` 累加（与 `report_yield` 同一条路），
  写失败只告警、不影响 crawl 退出码；
* **不进来**的语义：不写 `rows_attempted`（放弃的格没有 attempt）、也不写 `rows_new`。

发布顺序（不能颠倒）
--------------------
`migrate_stage` 在应用容器之前跑 `alembic upgrade head`，而 schema 闸对「DB 落后于镜像」
是**拒绝启动**。所以只能「先滚带 0025 的镜像建列，再滚写这一列的执行器」；
反过来的顺序会让执行器的 UPDATE 撞 `UndefinedColumn`（已被 try/except 兜住，但那一轮
就白丢了读数）。旧镜像看到 DB 在新 revision 属 `AHEAD_UNKNOWN`，按既定滚动窗口放行。

Revision ID: 0025_run_rows_abandoned
Revises: 0024_federated_runner_pin_c33bf67
"""

from alembic import op
import sqlalchemy as sa

revision = "0025_run_rows_abandoned"
down_revision = "0024_federated_runner_pin_c33bf67"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # 列在 models 里声明，sqlite 由 create_all 建；迁移链空转（既有约定）。
        return
    op.add_column("policy_runs", sa.Column("rows_abandoned", sa.Integer(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.drop_column("policy_runs", "rows_abandoned")
