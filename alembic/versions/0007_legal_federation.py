r"""Legal-line federation registration (change legal-line-federation, task 1.1/1.3).

Revision ID: 0007_legal_federation
Revises: 0006_control_plane_adoption

Two moves, both guarded so a re-run (or a database that already carries the
shapes) is a verified no-op — the 0006 adoption convention:

1. Schema: ``crawl_sources`` gains the federated-registration columns
   (``kind`` / ``runner_image`` / ``runner_command`` / ``timeout_seconds`` /
   ``frozen_reason``) and ``crawl_runs`` gains ``metrics`` (telemetry
   quality caliper: effective_body_ratio / coverage_count, D5). All
   ``ADD COLUMN IF NOT EXISTS``; existing rows default to kind='platform',
   which is exactly what the dispatcher-mirrored manifest sources are.

2. Seed: the law line's 8 execution bodies register into ``crawl_sources``
   under their CronJob names (= crawl_runs source names, zero-migration
   continuity, design D1), site xinru-server1 (the post-migration resident
   host), kind='federated'. The runner
   declarations mirror helm-law-scraw/values.yaml (2026-10-06 converged
   inventory): command = the CronJob argv, timeout = activeDeadlineSeconds,
   image = the chart's per-source resolved image with one deliberate
   divergence — flk takes the ccr wave image because dispatcher-created Jobs
   carry no imagePullSecrets (the chart's in-service Harbor ref would not
   pull there; see the seed comment below).
   wenshu-crawl registers frozen (frozen_reason, no runner declaration) —
   permanently un-triggersable while visible in the inventory.

The seed is an UPSERT (``ON CONFLICT DO UPDATE``) that re-asserts only the
federation-owned fields (site/kind/runner declaration/frozen). It never
touches dispatcher-owned fields (schedule / last_commit / auth_profile /
enabled) — the manifest sync stays authoritative for platform rows, and the
sync side must skip kind='federated' rows so the two writers never overlap
(design: 扩列与 manifest sync 共存). The xinru-master crawl_sites row is
adopted guardedly (DO NOTHING) so the FK resolves on every database the
chain builds; the site's own lifecycle stays with the sites registration.

PostgreSQL-only like the rest of the chain — SQLite test consumers build
from the models with ``create_all``. Downgrade drops the six added columns
and nothing else: the registration ROWS are data, not schema — dropping the
columns already reverts the federation semantics, and deleting live rows
could destroy operator state (run history keys off source names).
"""
from __future__ import annotations

import json

from alembic import op

revision = "0007_legal_federation"
down_revision = "0006_control_plane_adoption"
branch_labels = None
depends_on = None

_DDL = (
    "ALTER TABLE crawl_sources "
    "ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'platform'",
    "ALTER TABLE crawl_sources "
    "ADD COLUMN IF NOT EXISTS runner_image TEXT",
    "ALTER TABLE crawl_sources "
    "ADD COLUMN IF NOT EXISTS runner_command JSONB",
    "ALTER TABLE crawl_sources "
    "ADD COLUMN IF NOT EXISTS timeout_seconds INTEGER",
    "ALTER TABLE crawl_sources "
    "ADD COLUMN IF NOT EXISTS frozen_reason TEXT",
    "ALTER TABLE crawl_runs "
    "ADD COLUMN IF NOT EXISTS metrics JSONB",
)

# The federated member site (task 1.2 registers it in sites.yaml; the seed
# adopts the row guardedly so the crawl_sources FK resolves everywhere).
# xinru-server1: the law line's resident host after the 2026-10 migration off
# xinru-master — fresh installs must seed the new reality.
_SITE_ROW = ("xinru-server1",
             "保留建制法律线驻留站 (xinru k3s, ns scraw, host xinru-server1) "
             "— the law line's reserved-installation site; the platform "
             "never schedules crawls here, only queues federated "
             "pending_runs",
             "k8s")

# Image refs from helm-law-scraw values.yaml (2026-10-06 live truth), with ONE
# deliberate divergence: flk's runner declaration uses the ccr wave image
# (_IMG_FDK) although the in-service chart CronJob keeps its Harbor mirror
# image (images.flkLawCrawl + harbor-lawcraw-pull). Dispatcher-created Jobs
# carry no imagePullSecrets, so the Harbor ref would fail to pull there; the
# declaration governs only dispatcher-triggered runs — the chart template
# (unchanged) keeps serving the daily cron from Harbor.
_IMG_FDK = "ccr.ccs.tencentyun.com/lawcraw-business/fd-law-data:sha-8433e3d"
_IMG_BROKER = "ccr.ccs.tencentyun.com/lawcraw-business/legal-auth-broker:sha-4b79ec5"

# The 8 law-line execution bodies, keyed by CronJob name (= crawl_sources
# primary key = crawl_runs.source). runner_image/runner_command/
# timeout_seconds mirror the chart; wenshu-crawl is frozen with no runner
# declaration (the monthly corpus-pack route replaced the night crawl).
# runner_env_from is the dispatcher's per-Job envFrom secret list (mechanical
# injection, not source business logic): every crawler needs the RustFS
# credentials secret or it crashes at startup (drill 4.2 BackoffLimitExceeded);
# rmfyalk additionally carries the auth-broker token/jar and its identity
# proxy secret. This constant is the single registration data home — 0008
# adds the column and backfills the rows FROM it (the column does not exist
# at this point in the chain, so the upsert below must not write it).
_CRED = ["flk-law-crawl-credentials"]
SEED_SOURCES = (
    # flk: ccr image here on purpose (see above) — Harbor in the chart
    {"source": "flk-law-crawl", "runner_image": _IMG_FDK,
     "runner_command": ["node", "bin/flk-crawl.mjs"],
     "timeout_seconds": 14400, "frozen_reason": None,
     "runner_env_from": list(_CRED)},
    {"source": "guide-cases-crawl", "runner_image": _IMG_FDK,
     "runner_command": ["node", "bin/guide-cases-crawl.mjs"],
     "timeout_seconds": 14400, "frozen_reason": None,  # chart-external object
     "runner_env_from": list(_CRED)},
    {"source": "rmfyalk-case-crawl", "runner_image": _IMG_BROKER,
     "runner_command": ["node", "bin/rmfyalk-crawl.mjs", "--account-id",
                        "acct001", "--auth-broker",
                        "http://legal-auth-broker.scraw", "--rate-ms", "1100"],
     "timeout_seconds": 21600, "frozen_reason": None,
     "runner_env_from": ["flk-law-crawl-credentials", "legal-auth-broker",
                         "legal-identity-rmfyalk-acct001"]},
    {"source": "mfa-treaty-crawl", "runner_image": _IMG_FDK,
     "runner_command": ["node", "bin/mfa-treaty-crawl.mjs", "--rate-ms", "1100"],
     "timeout_seconds": 28800, "frozen_reason": None,
     "runner_env_from": list(_CRED)},
    {"source": "gov-rules-crawl", "runner_image": _IMG_FDK,
     "runner_command": ["node", "bin/gov-rules-crawl.mjs", "--rate-ms", "1100"],
     "timeout_seconds": 14400, "frozen_reason": None,
     "runner_env_from": list(_CRED)},
    {"source": "party-regulation-crawl", "runner_image": _IMG_FDK,
     "runner_command": ["node", "bin/party-regulation-crawl.mjs",
                        "--rate-ms", "1100"],
     "timeout_seconds": 14400, "frozen_reason": None,
     "runner_env_from": list(_CRED)},
    {"source": "ccdi-supervisory-crawl", "runner_image": _IMG_FDK,
     "runner_command": ["node", "bin/ccdi-supervisory-crawl.mjs",
                        "--rate-ms", "1100"],
     "timeout_seconds": 28800, "frozen_reason": None,
     "runner_env_from": list(_CRED)},
    {"source": "wenshu-crawl", "runner_image": None, "runner_command": None,
     "timeout_seconds": None,
     "frozen_reason": "月度语料包路线（夜跑永久停）",
     "runner_env_from": list(_CRED)},
)

_SEED_SITE_SQL = (
    "INSERT INTO crawl_sites (id, description, kind, enabled) "
    f"VALUES ('{_SITE_ROW[0]}', '{_SITE_ROW[1].replace(chr(39), chr(39) * 2)}', "
    f"'{_SITE_ROW[2]}', true) "
    "ON CONFLICT (id) DO NOTHING"
)


def _upsert_sql(row: dict) -> str:
    """Federation-owned-field upsert for one member row (idempotent).

    Dispatcher-owned fields (schedule / last_commit / auth_profile / enabled
    / updated_at) are deliberately absent from the UPDATE set, and so is
    runner_env_from — that column is created and backfilled by 0008 (it does
    not exist at this point in the chain), whose UPDATE reads it from
    SEED_SOURCES directly.
    """
    cmd = json.dumps(row["runner_command"]) if row["runner_command"] else None

    def q(v: str | None) -> str:
        if v is None:
            return "NULL"
        return "'" + str(v).replace("'", "''") + "'"

    return (
        "INSERT INTO crawl_sources (source, site, kind, runner_image, "
        "runner_command, timeout_seconds, frozen_reason) "
        f"VALUES ({q(row['source'])}, '{_SITE_ROW[0]}', 'federated', "
        f"{q(row['runner_image'])}, {q(cmd)}::jsonb, "
        f"{row['timeout_seconds'] if row['timeout_seconds'] is not None else 'NULL'}, "
        f"{q(row['frozen_reason'])}) "
        "ON CONFLICT (source) DO UPDATE SET "
        "site = EXCLUDED.site, kind = EXCLUDED.kind, "
        "runner_image = EXCLUDED.runner_image, "
        "runner_command = EXCLUDED.runner_command, "
        "timeout_seconds = EXCLUDED.timeout_seconds, "
        "frozen_reason = EXCLUDED.frozen_reason"
    )


# Reverse of _DDL: only the columns this revision added.
_DOWNGRADE_DROPS = (
    "ALTER TABLE crawl_runs DROP COLUMN IF EXISTS metrics",
    "ALTER TABLE crawl_sources DROP COLUMN IF EXISTS frozen_reason",
    "ALTER TABLE crawl_sources DROP COLUMN IF EXISTS timeout_seconds",
    "ALTER TABLE crawl_sources DROP COLUMN IF EXISTS runner_command",
    "ALTER TABLE crawl_sources DROP COLUMN IF EXISTS runner_image",
    "ALTER TABLE crawl_sources DROP COLUMN IF EXISTS kind",
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return  # sqlite tests build via create_all (chain convention)
    for statement in _DDL:
        bind.exec_driver_sql(statement)
    bind.exec_driver_sql(_SEED_SITE_SQL)
    for row in SEED_SOURCES:
        bind.exec_driver_sql(_upsert_sql(row))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for statement in _DOWNGRADE_DROPS:
        bind.exec_driver_sql(statement)
