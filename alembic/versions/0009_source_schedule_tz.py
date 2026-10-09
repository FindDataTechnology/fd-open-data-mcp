r"""Per-source schedule timezone (change prearm-login-station, task 1.1).

Revision ID: 0009_source_schedule_tz
Revises: 0008_federation_runner_env

``crawl_sources`` gains ``schedule_tz`` (TEXT NULL): the timezone the
source's ``schedule`` cron is matched in, so a login-gated source can be
calibrated to the operator's night (e.g. rmfyalk at Beijing 03:10, inside the
human re-login window) without shifting the whole fleet — NULL keeps the
historical UTC semantics every existing source is calibrated to.

The column was first created out-of-band by the fd-industry-data dispatcher
(``fd_industry_data/dispatch.py``: ``ALTER TABLE crawl_sources ADD COLUMN IF
NOT EXISTS schedule_tz text``), which is why the guarded add here is a
verified no-op on production and on any database the dispatcher has already
touched; a fresh database built from this chain alone gets the shape the
Console's pre-arm planner (``station_ops.prearm_plan``) reads.

An unknown/invalid timezone is never an error: the Console degrades it to UTC
(the dispatcher's ``_schedule_now`` does the same), so a typo cannot silence a
source.

PostgreSQL-only like the rest of the chain — SQLite test consumers build from
the models with ``create_all``. Downgrade drops the column only.
"""
from __future__ import annotations

from alembic import op

revision = "0009_source_schedule_tz"
down_revision = "0008_federation_runner_env"
branch_labels = None
depends_on = None

_DDL = (
    "ALTER TABLE crawl_sources "
    "ADD COLUMN IF NOT EXISTS schedule_tz TEXT",
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return  # sqlite tests build via create_all (chain convention)
    for statement in _DDL:
        bind.exec_driver_sql(statement)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    bind.exec_driver_sql(
        "ALTER TABLE crawl_sources DROP COLUMN IF EXISTS schedule_tz")
