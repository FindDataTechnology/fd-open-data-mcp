r"""Price active pool: which symbols are worth pre-warming.

Revision ID: 0012_price_active_pool
Revises: 0011_federated_runner_tag

Capability ``price-active-pool`` (design D5). The pool is the "B" half of the
on-demand price decision: gap filling (A) serves any symbol a caller asks for,
but its first request pays an upstream round trip. The pool names the symbols
worth paying for in advance, so their first request is a cache hit.

Two sources share one table, recorded as a ``source`` column rather than a
boolean: a symbol can be both configured *and* frequently requested, and an
operator asking "why is this warm?" needs the reason, not a boolean.

PostgreSQL-only like the rest of the chain (SQLite test consumers build from
the models with ``create_all``).

Downgrade drops the table: it is metadata derived from usage, and rebuilding it
is a side effect of normal traffic.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0012_price_active_pool"
down_revision = "0011_federated_runner_tag"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "price_pool_members",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=16), server_default="none", nullable=False),
        sa.Column("request_count", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("last_requested_at", sa.DateTime(), nullable=True),
        sa.Column("added_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("price_pool_members_pkey")),
        sa.UniqueConstraint("entity_type", "entity_id", name="uq_price_pool_member"),
        sa.CheckConstraint("source in ('config','frequency','none')",
                           name="price_pool_members_source_check"),
    )
    op.create_index("price_pool_members_source_idx", "price_pool_members",
                    ["source"], unique=False)


    op.create_table(
        "price_empty_intervals",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("concept_id", sa.Integer(), nullable=False),
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("start", sa.String(length=10), nullable=False),
        sa.Column("end", sa.String(length=10), nullable=False),
        sa.Column("probed_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("price_empty_intervals_pkey")),
        sa.UniqueConstraint("concept_id", "entity_type", "entity_id", "start", "end",
                            name="uq_price_empty_interval"),
    )
    op.create_index("price_empty_intervals_lookup_idx", "price_empty_intervals",
                    ["concept_id", "entity_type", "entity_id"], unique=False)


def downgrade() -> None:
    op.drop_index("price_empty_intervals_lookup_idx",
                  table_name="price_empty_intervals")
    op.drop_table("price_empty_intervals")
    op.drop_index("price_pool_members_source_idx", table_name="price_pool_members")
    op.drop_table("price_pool_members")
