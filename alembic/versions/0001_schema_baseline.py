"""Schema baseline: the verified create_all snapshot of 2026-09-18.

Revision ID: 0001_schema_baseline

This revision supersedes the former 001..009 chain (now in alembic/attic/).
No environment ever had an ``alembic_version`` table, so those revisions
executed nowhere; the live schema was shaped by ``create_all()`` plus manual
scripts. The baseline is that state: upgrade() executes
``alembic/schema_baseline.sql`` verbatim — the artifact generated from
``Base.metadata.create_all()`` on an empty PostgreSQL 14 database and verified
column-for-column against the canonical ``fd_open_data`` (27 tables, zero
differences, 2026-09-18). Keeping the .sql file as the executed content means
the revision cannot drift from the verified artifact.

PostgreSQL only: the snapshot is pg_dump output. SQLite consumers (tests)
build their schema with an explicit test builder, not by running migrations.

Lines starting with a backslash are psql meta-commands emitted by newer
pg_dump versions (``\\restrict`` guards); psql interprets them, the driver
cannot, so they are stripped before execution. The dump's
``SELECT pg_catalog.set_config('search_path', '', false)`` is stripped for the
same reason — it mutates session state and would break Alembic's own
unqualified ``alembic_version`` bookkeeping that runs after this migration,
and every object in the snapshot is schema-qualified anyway. No statement in
the snapshot needs to run outside a transaction (no CONCURRENTLY, no COPY).
"""
from __future__ import annotations

import re

from alembic import op

revision = "0001_schema_baseline"
down_revision = None
branch_labels = None
depends_on = None

_CREATE_TABLE = re.compile(r"^CREATE TABLE (?:IF NOT EXISTS )?public\.(\w+) \(", re.M)
_SEARCH_PATH_RESET = "SELECT pg_catalog.set_config('search_path', '', false);"


def _snapshot_sql() -> str:
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "schema_baseline.sql"
    text = path.read_text(encoding="utf-8")
    return "\n".join(
        line
        for line in text.splitlines()
        if not line.startswith("\\") and line.strip() != _SEARCH_PATH_RESET
    )


def upgrade() -> None:
    op.get_bind().exec_driver_sql(_snapshot_sql())


def downgrade() -> None:
    # pg_dump emits tables in dependency order, so reverse file order is a
    # valid drop order. Baseline downgrade is a full teardown; per D6 the
    # operational rollback is redeploying the previous image, not this.
    names = _CREATE_TABLE.findall(_snapshot_sql())
    for name in reversed(names):
        op.execute(f'DROP TABLE IF EXISTS public."{name}"')
