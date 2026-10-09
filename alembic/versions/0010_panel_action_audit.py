r"""Panel action audit table (change panel-rbac-i18n-refresh, task 3.1).

Revision ID: 0010_panel_action_audit
Revises: 0009_source_schedule_tz

One row per audited panel action: every POST the panel serves (trigger /
cancel / toggle / save / delete / run-now / capacity / launch / ...) plus
the login-callback events (success and refusal). Actor attribution is
decided by the auth middleware — a session caller records ``actor_sub`` +
display name in ``actor_name``, a token caller records a NULL sub and the
literal ``"token"``; ``target`` carries the acted-on id when the handler
knows one; ``outcome`` is success|failure with the failure detail in
``reason``.

Unlike the rest of the chain (PostgreSQL-only guarded DDL; SQLite test
consumers build from the models with ``create_all``), this revision is
written with portable ``op`` constructs, per the change spec: the SQLite
test database runs the chain itself, so the migration must be
dialect-compatible. On PostgreSQL it creates the same shape the model
declares: naive-UTC ``ts`` (TIMESTAMP WITHOUT TIME ZONE — the writer
supplies UTC via the ORM default, so there is no server default, matching
the discovery-mirror family), bounded VARCHAR columns, TEXT ``reason``,
and the single ``ix_panel_action_audit_ts`` index (the name
``index=True`` generates on the model). No IF NOT EXISTS guard, unlike
the adopted tables: this table has no out-of-band bootstrap path, so a
duplicate create is a real error worth failing on. Downgrade drops the
index and the table; nothing else references them.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010_panel_action_audit"
down_revision = "0009_source_schedule_tz"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "panel_action_audit",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("actor_sub", sa.String(length=255), nullable=True),
        sa.Column("actor_name", sa.String(length=255), nullable=False),
        sa.Column("route", sa.String(length=255), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target", sa.String(length=255), nullable=True),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("ts", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_panel_action_audit_ts", "panel_action_audit", ["ts"])


def downgrade() -> None:
    op.drop_index("ix_panel_action_audit_ts", table_name="panel_action_audit")
    op.drop_table("panel_action_audit")
