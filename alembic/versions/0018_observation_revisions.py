r"""Observation revisions (define-ingest-revision-and-coverage).

Revision ID: 0018_observation_revisions
Revises: 0017_census_bigint

为什么需要这张表
----------------
``fetch/cache.py`` 的 ``write_cache_range`` 是原地 upsert：当前期观测值被新抓
取替换时，旧值直接消失。而 ``_period_final`` 已经实现了"周期走完即定型"——定
型后的事实永不重取，定型前的覆写却没有任何留痕。于是同一份数据有两种待遇，
而缺的正是"定型前改了什么"这一半。

两种修订形态（design D1 / D2）
------------------------------
* ``overwrite``：单点修订，每行一条，带旧值/新值/来源。
* ``batch``：整段修订（前复权序列在每次分红后上游整体重写）。逐点留痕的成本
  与价值不成比例，因此一个批次记一行（范围 + 点数 + 原因），逐点旧值不存。

表是只追加的：不更新、不删除（保留期清理是唯一的删除路径，且必须显式配置）。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0018_observation_revisions"
down_revision = "0017_census_bigint"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite consumers build from the models (create_all) — see tests/conftest.
        return

    op.create_table(
        "observation_revisions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("concept_id", sa.Integer(), nullable=False),
        sa.Column("entity_type", sa.String(32), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("date", sa.String(64), nullable=False),
        sa.Column("granularity", sa.String(8), nullable=False, server_default="day"),
        sa.Column("source_used", sa.String(64), nullable=False),
        sa.Column("revision_kind", sa.String(16), nullable=False, server_default="overwrite"),
        sa.Column("old_value", sa.String(255), nullable=True),
        sa.Column("new_value", sa.String(255), nullable=True),
        sa.Column("batch_id", sa.String(64), nullable=True),
        sa.Column("range_start", sa.String(64), nullable=True),
        sa.Column("range_end", sa.String(64), nullable=True),
        sa.Column("point_count", sa.Integer(), nullable=True),
        sa.Column("reason", sa.String(128), nullable=True),
        sa.Column("revised_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("revision_kind IN ('overwrite','batch')",
                           name="ck_obs_revision_kind"),
    )
    op.create_index("ix_obs_revision_coord", "observation_revisions",
                    ["concept_id", "entity_type", "entity_id", "date", "granularity"])
    op.create_index("ix_obs_revision_batch", "observation_revisions", ["batch_id"])
    op.create_index("ix_obs_revision_time", "observation_revisions", ["revised_at"])


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.drop_index("ix_obs_revision_time", table_name="observation_revisions")
    op.drop_index("ix_obs_revision_batch", table_name="observation_revisions")
    op.drop_index("ix_obs_revision_coord", table_name="observation_revisions")
    op.drop_table("observation_revisions")
