r"""Federated runner envFrom (change legal-line-federation, drill 4.2 follow-up).

Revision ID: 0008_federation_runner_env
Revises: 0007_legal_federation
branch_labels = None
depends_on = None

Drill 4.2 found the gap: a dispatcher-triggered federated Job had a command
but no environment, and the crawler crashed on missing RustFS credentials
(BackoffLimitExceeded). ``crawl_sources`` therefore gains
``runner_env_from`` (JSONB NULL): the list of Secret names the dispatcher
envFrom's into that source's Job — mechanical injection, not source business
logic.

One data home: the per-source lists live in 0007's ``SEED_SOURCES``
registration constant; this revision only adds the column and backfills the
8 federated rows FROM it (every source needs at least
flk-law-crawl-credentials; rmfyalk additionally carries the auth-broker
token/jar secrets and its identity proxy). Statements are idempotent by
nature (ADD COLUMN IF NOT EXISTS, same-value UPDATEs scoped to
kind='federated'), so a re-run — or the migrate stage on a database whose
rows were already backfilled out-of-band — is a verified no-op.

PostgreSQL-only like the rest of the chain — SQLite test consumers build
from the models with ``create_all``. Downgrade drops the column only: env
data is derived from the registration constant, so nothing else needs
undoing.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from alembic import op

revision = "0008_federation_runner_env"
down_revision = "0007_legal_federation"
branch_labels = None
depends_on = None

_DDL = (
    "ALTER TABLE crawl_sources "
    "ADD COLUMN IF NOT EXISTS runner_env_from JSONB",
)

# The registration constant is the single data home (see module docstring):
# load 0007 by path — revisions are not a package — and derive the UPDATEs
# from it, so the seed and this backfill can never drift apart.
_SPEC = importlib.util.spec_from_file_location(
    "mig_0007", Path(__file__).with_name("0007_legal_federation.py"))
_MOD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MOD)
_SEED = _MOD.SEED_SOURCES


def _update_sql(row: dict) -> str:
    env = json.dumps(row["runner_env_from"]) if row["runner_env_from"] else None
    literal = "NULL" if env is None else (
        "'" + env.replace("'", "''") + "'::jsonb")
    return (f"UPDATE crawl_sources SET runner_env_from = {literal} "
            f"WHERE source = '{row['source']}' AND kind = 'federated'")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return  # sqlite tests build via create_all (chain convention)
    for statement in _DDL:
        bind.exec_driver_sql(statement)
    for row in _SEED:
        bind.exec_driver_sql(_update_sql(row))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    bind.exec_driver_sql(
        "ALTER TABLE crawl_sources DROP COLUMN IF EXISTS runner_env_from")
