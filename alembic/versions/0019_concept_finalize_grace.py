r"""concepts.finalize_grace_days — per-concept 定型窗口声明。

为什么需要这一列
----------------
`fetch/finalization.py` 的 `resolve_window(granularity, concept)` 会读
`concept.finalize_grace_days` 作为粒度默认之上的覆盖值。但生产库此前**没有这一列**：
`getattr` 永远拿到 `None`，于是概念级窗口在任何环境下都不可达，只能落到粒度默认。

这正是"门口修好了但没挂载"的形态——代码里的通路存在、单测也过，生产上却走不到。
本迁移把声明位补上，通路才算真的接上。

语义
----
* `NULL`  = 未声明 → 由 `fetch/finalization.py` 的粒度默认兜底，并进巡检待补清单。
* `>= 0`  = 声明值（天），在该粒度的**周期基数**上追加。注意年度粒度的基数是
  **次年末**，故 `0` 表示"次年整年可修订、第三年元旦定型"，而不是"跨年即定型"。
* 不加默认值：用默认值会让"未声明"与"声明为 0"变成同一件事，而它们的含义完全不同
  （前者应进待补清单，后者是一个明确决定）。

Revision ID: 0019_concept_finalize_grace
Revises: 0018_observation_revisions
"""

from alembic import op
import sqlalchemy as sa

revision = "0019_concept_finalize_grace"
down_revision = "0018_observation_revisions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SQLite 上这张表根本不存在（0001 基线是 pg_dump 产物，见其 upgrade 开头的
    # dialect 判断；sqlite 的测试库由显式 test builder 建表，不走迁移链）。
    # 少了这道判断，整条 `alembic upgrade head` 在 sqlite 上死在这一步——
    # tests/test_vector_migration_sql.py::test_alembic_upgrade_head_on_sqlite_is_guarded
    # 正是这么红的（2026-11-12 实测）。
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.add_column(
        "concepts",
        sa.Column("finalize_grace_days", sa.Integer(), nullable=True),
    )
    # 只索引"已声明"的行：待补清单查的是 IS NULL，那个方向不需要索引。
    op.create_index(
        "ix_concepts_finalize_grace",
        "concepts",
        ["finalize_grace_days"],
        postgresql_where=sa.text("finalize_grace_days IS NOT NULL"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.drop_index("ix_concepts_finalize_grace", table_name="concepts")
    op.drop_column("concepts", "finalize_grace_days")
