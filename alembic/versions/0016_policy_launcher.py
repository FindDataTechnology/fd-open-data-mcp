r"""CrawlPolicy execution-model column (add-data-stream-engine task 4.1).

Revision ID: 0016_policy_launcher
Revises: 0015_obs_month_dist

``crawl_policies.launcher`` — auto|job|leasing. The default ``auto`` keeps the
existing per-plan Job/scrapyd path byte-for-byte, so no existing policy changes
behavior (spec worker-leasing「默认配置不改变存量行为」). ``leasing`` is the
opt-in scale path: the reconciler splits the plan into work items and enqueues
them to the lease queue instead of launching one Job.

server_default='auto' so existing rows (and raw-SQL writers) get the value on
backfill; the column is NOT NULL. PostgreSQL-only like the rest of the chain
(SQLite test consumers build from the models with create_all).
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0016_policy_launcher"
down_revision = "0015_obs_month_dist"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.add_column(
        "crawl_policies",
        sa.Column("launcher", sa.String(length=16), nullable=False,
                  server_default="auto"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.drop_column("crawl_policies", "launcher")
