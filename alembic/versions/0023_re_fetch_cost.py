r"""re_fetch_cost — 重取代价的三态（一次 HTTP / 需重建 / 不可重建）。

为什么加这两列
--------------
`establish-data-retention-principle` 的 R1 写明：每个源接入时必须声明五项，其中一项是
**`re_fetch_cost`（重取代价：一次 HTTP / 需重建 / 不可重建）**；R5 又写：对"需重建"的层，
淘汰前 MUST 给出重算成本估算（耗时 + 上游调用量），"仅有容量收益、没有成本数字的淘汰
MUST NOT 被批准"。

而库里只有布尔 `re_fetchable`——**这条要求此前无处落点**。归档会把一条引用不存在字段的
要求写进主规格，正是本项目最忌的"声明≠挂载"。

这个缺口不是纸面的，2026-11-11 就已经咬过一次：`akshare/aum` 既不是"一次 HTTP"
也不是"不可重建"，它是**可重取但需要重算 47 天**（tasks 5.1 实测 0.1 行/秒 × 40 万行）。
布尔装不下第三态，只能拿 `re_fetchable=false` 将就——那等于**在契约里写一句假话**，
而契约的全部价值就在于"读它的人可以照着做判断"。

三态的语义（由审计与淘汰工具实现，不在库里写死行为）
----------------------------------------------------
* `one_http`    —— 一次调用即可取回，删了随时补回来
* `rebuild`     —— 能取回，但需要重算；**淘汰前必须给出耗时估算**，否则不批
* `impossible`  —— 取不回来（上游已下线 / 只售当期快照 / 原始抓取），一行都不许动

`re_fetch_cost_hours` 存那份估算（小时）。它是 `rebuild` 的伴生证据，不是装饰：
淘汰工具会在拒绝时把小时数报出来，让人拿"释放多少 MB"去对"重算多少小时"。
"上游调用量"不落列——它由 policy_runs 的实测吞吐反推（tasks 5.1 就是这么算的），
存一份静态数字只会过期。

回填只做无歧义的映射：显式 `false → impossible`、显式 `true → one_http`、
`NULL` 保持 `NULL`（未声明就要一直被点名，不能拿默认值糊过去）。
`rebuild` **不自动推断**——它必须由人对着实测数字写下来，本次只有 aum 一条。

Revision ID: 0023_re_fetch_cost
Revises: 0022_obs_month_derived
"""

from alembic import op
import sqlalchemy as sa

revision = "0023_re_fetch_cost"
down_revision = "0022_obs_month_derived"
branch_labels = None
depends_on = None

_COST_DOMAIN = "('one_http', 'rebuild', 'impossible')"


def upgrade() -> None:
    op.add_column("data_retention_contracts",
                  sa.Column("re_fetch_cost", sa.String(length=16)))
    op.add_column("data_retention_contracts",
                  sa.Column("re_fetch_cost_hours", sa.Integer()))

    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    # 只用无歧义的映射回填；NULL 保持 NULL（"没声明"必须继续被审计点名）。
    bind.execute(sa.text(f"""
        UPDATE data_retention_contracts
           SET re_fetch_cost = CASE
                 WHEN re_fetchable IS FALSE THEN 'impossible'
                 WHEN re_fetchable IS TRUE  THEN 'one_http'
                 ELSE NULL
               END
         WHERE re_fetch_cost IS NULL
    """))

    # 值域闸：拼错的 'rebuildable' 之类会让淘汰工具静默当成"未声明"而放行。
    op.create_check_constraint(
        "ck_retention_re_fetch_cost",
        "data_retention_contracts",
        f"re_fetch_cost IS NULL OR re_fetch_cost IN {_COST_DOMAIN}",
    )

    # 唯一一条需要人对着实测数字写的：aum 可重取，但要重算约 47 天。
    # 同时把布尔改回 true——它本来就是可重取的，之前写 false 是因为布尔装不下
    # "很贵"这一态；现在有三态了，假话该收回。
    bind.execute(sa.text("""
        UPDATE data_retention_contracts
           SET re_fetchable = TRUE,
               re_fetch_cost = 'rebuild',
               re_fetch_cost_hours = 1128,
               updated_at = now()
         WHERE source = 'akshare' AND family = 'aum'
    """))


def downgrade() -> None:
    op.drop_constraint("ck_retention_re_fetch_cost",
                       "data_retention_contracts", type_="check")
    op.drop_column("data_retention_contracts", "re_fetch_cost_hours")
    op.drop_column("data_retention_contracts", "re_fetch_cost")
