r"""retention exemption — 窗口之外的保留必须显式（理由 / 责任人 / 到期）。

为什么加这三列
--------------
`establish-data-retention-principle` 有一条 MUST：默认必须是有限窗口，
**窗口之外的保留必须有显式豁免记录**（用途 + 到期 + 责任人），不得沉默保留。
在它之前，"这条族为什么还留着五年前的数据"只能靠翻聊天记录回答；
没有到期字段，豁免又会变成永久豁免——和"永久保留"没有区别。

三列挂在 `data_retention_contracts` 上（不是新表），因为豁免的作用域与契约相同
（源 × 族）：契约说"该留多久"，豁免说"为什么这次多留、留到哪天"。
拆成两张表会让"一条族的完整声明"要 join 才读得出。

语义（由审计与淘汰工具实现，不在库里写死）
------------------------------------------
* `exempt_until IS NULL`            → 无豁免，超窗数据 = **可淘汰**
* `exempt_until >= today`           → 有效豁免，超窗数据标 **保留**（并报出理由/责任人）
* `exempt_until <  today`           → 已过期，回到 **可淘汰**，并在报告里点名"过期未清理"

全可空、无默认值：与 0020 的取舍一致——`NULL` 是"没声明"，不是"声明为永久"。

Revision ID: 0021_retention_exemption
Revises: 0020_retention_contracts
"""

from alembic import op
import sqlalchemy as sa

revision = "0021_retention_exemption"
down_revision = "0020_retention_contracts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("data_retention_contracts", sa.Column("exempt_reason", sa.Text()))
    op.add_column("data_retention_contracts",
                  sa.Column("exempt_owner", sa.String(length=128)))
    op.add_column("data_retention_contracts", sa.Column("exempt_until", sa.Date()))


def downgrade() -> None:
    # 先删到期时间：它是"这条豁免还成不成立"的唯一判据，留着另两列只会产生
    # 一条没有终点的豁免（正是本次要禁止的形态）。
    op.drop_column("data_retention_contracts", "exempt_until")
    op.drop_column("data_retention_contracts", "exempt_owner")
    op.drop_column("data_retention_contracts", "exempt_reason")
