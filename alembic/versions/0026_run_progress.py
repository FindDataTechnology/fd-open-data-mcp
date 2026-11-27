r"""run_progress — 运行行记「被杀时在哪一个标的」。

为什么加这一列
--------------
`add-crawl-job-deadline` 让 deadline 杀死的 Job 能在 `policy_runs.detail` 上念出
「超时多少秒」。但 2026-11-16 起 policy 2659 每天跑满 180min 被杀，detail 永远只有
「deadline exceeded after 10800s」——读得出「超时」，读不出「卡在哪个符号」。执行体
（direct 脚本与 scrapy 管道）此前从不落「正在处理谁」，pod 被 backoff/TTL 回收后，
唯一能回答这个问题的现场也一并消失。

口径
----
* **可空**，且缺失表示「这一轮从未报过进度」——只在这种情形下 reconciler 才把
  detail 标成 `reason-opaque`（与 `rows_*` 同一 NULL 约定，绝不拿 NULL 读成「进度.
  为 0」）；
* 由执行体按 `SCRAW_JOB_REF` 自报，格式 `"<标的>|<已完成>/<计划>"`（`plan` 未知时留
  空），写失败只告警、不影响执行体退出码（与 `report_run_yield` 同一条路）；
* reconciler 关闭 `deadline_exceeded` 时读这一列拼进 detail，之后清空（下一轮重跑
  从空开始，不会带着上一轮的旧位置）。

发布顺序（不能颠倒）
--------------------
与 0025 相同：`migrate_stage` 在应用容器之前跑 `alembic upgrade head`，schema 闸对
「DB 落后于镜像」是拒绝启动。所以「先滚带 0026 的镜像建列，再滚写这一列的执行体」；
旧镜像看到 DB 在新 revision 属 `AHEAD_UNKNOWN`，按既定滚动窗口放行。

Revision ID: 0026_run_progress
Revises: 0025_run_rows_abandoned
"""

from alembic import op
import sqlalchemy as sa

revision = "0026_run_progress"
down_revision = "0025_run_rows_abandoned"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # 列在 models 里声明，sqlite 由 create_all 建；迁移链空转（既有约定）。
        return
    op.add_column("policy_runs", sa.Column("progress", sa.String(length=255), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.drop_column("policy_runs", "progress")
