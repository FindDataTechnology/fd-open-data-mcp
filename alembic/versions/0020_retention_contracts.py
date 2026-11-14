r"""data_retention_contracts — 留存契约落库（源 × 概念族）。

为什么要有这张表
----------------
`establish-data-retention-principle` 的 R1 要求"每个源接入时声明留存契约"。
在它之前，契约只活在我写的 `scripts/audit_retention_contracts.py` 的常量里——
那是 bootstrap，不是制度：审计能查、但写入路径读不到它，也就拦不住新数据。

设计取舍
--------
* **族级而非源级**：同一个源内部差异极大。实测 akshare 的 `financials` 要全历史
  （退市公司历史用于去幸存者偏差），而 `tenure_days` 只该留当期（它是
  "今天 − 任职起始日"，逐日重存纯浪费）。挂在源上没有意义。
* **`family` 允许 `*`**：作为该源的默认值，未单列的族回落到它。
* **五字段全可空，不给默认值**：`NULL` 表示"未声明"，进待补清单。
  给默认值会让"没想过"和"想过并决定永久保留"变成同一件事——这正是本项目
  反复出问题的那种混淆（`0019` 里对 `finalize_grace_days` 用了同一条理由）。
* **不加 CHECK 约束去限制 class 取值**：class 是制度用词（P/F/W/T），
  硬约束在代码里更易演进；但窗口与 class 的矛盾（P 类带窗口）由审计报出来。

Revision ID: 0020_retention_contracts
Revises: 0019_concept_finalize_grace
"""

from alembic import op
import sqlalchemy as sa

revision = "0020_retention_contracts"
down_revision = "0019_concept_finalize_grace"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "data_retention_contracts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        # 源名用文本而非外键：契约要能先于源记录存在（新源接入时先声明再爬）
        sa.Column("source", sa.String(length=64), nullable=False),
        # '*' = 该源默认；否则为概念码第一段（如 financials / price / tenure_days）
        sa.Column("family", sa.String(length=64), nullable=False),
        # P 永久 / F 全历史 / W 滚动窗口 / T 只留当期
        sa.Column("retention_class", sa.String(length=8), nullable=True),
        sa.Column("retention_window_days", sa.Integer(), nullable=True),
        sa.Column("re_fetchable", sa.Boolean(), nullable=True),
        sa.Column("upstream_history_mutable", sa.Boolean(), nullable=True),
        sa.Column("volume_budget_mb", sa.Integer(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("source", "family", name="uq_retention_contract_scope"),
    )
    # 巡检与写入闸门都按 (source, family) 精确查
    op.create_index(
        "ix_retention_contracts_scope",
        "data_retention_contracts",
        ["source", "family"],
    )


def downgrade() -> None:
    op.drop_index("ix_retention_contracts_scope", table_name="data_retention_contracts")
    op.drop_table("data_retention_contracts")
