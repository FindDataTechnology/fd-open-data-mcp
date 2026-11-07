r"""Census size counters to BIGINT (widen-census-bigint).

Revision ID: 0017_census_bigint
Revises: 0016_policy_launcher

``data_census.approx_rows`` and ``total_size_bytes`` were ``integer`` (max
2,147,483,647). The observation store grew past that on 2026-11-08
(``total_size_bytes = 2,873,810,944``), so every census refresh since
2026-11-03 18:07 UTC aborted with ``NumericValueOutOfRange: integer out of
range``. The reconciler treats a census failure as a warning, so the only
visible symptom was a frozen ``sampled_at``.

``ALTER COLUMN TYPE bigint`` widens in place — PostgreSQL does not rewrite the
table for int4 -> int8, so this is safe to run on the live database.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0017_census_bigint"
down_revision = "0016_policy_launcher"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite test consumers build from the models (create_all); Apache
        # Derby-style type mismatches are not in play there.
        return
    op.alter_column("data_census", "approx_rows",
                    existing_type=sa.Integer(), type_=sa.BigInteger(),
                    existing_nullable=True)
    op.alter_column("data_census", "total_size_bytes",
                    existing_type=sa.Integer(), type_=sa.BigInteger(),
                    existing_nullable=True)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.alter_column("data_census", "approx_rows",
                    existing_type=sa.BigInteger(), type_=sa.Integer(),
                    existing_nullable=True)
    op.alter_column("data_census", "total_size_bytes",
                    existing_type=sa.BigInteger(), type_=sa.Integer(),
                    existing_nullable=True)
