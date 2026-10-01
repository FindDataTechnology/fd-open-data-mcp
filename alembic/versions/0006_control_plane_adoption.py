r"""Adopt the externally-bootstrapped control-plane tables (schema-drift-closure).

Revision ID: 0006_control_plane_adoption
Revises: 0005_concept_coverage

2026-10-01: `alembic check` revealed 15 model tables living entirely outside
the versioned chain — created out-of-band on production, invisible to a fresh
database, and masked from drift detection by the EXTERNALLY_MANAGED_TABLES
bridge in ``alembic/env.py`` (commit 74963fa). This revision adopts them;
the exemption entries for model-declared tables are removed alongside it, so
model drift for this family fails ``alembic check`` again.

Division of labour across the chain (the "15 tables" of the proposal):
  - registry_entries              -> guarded create added to 0003 (its FK
                                     ordering demanded it; see that revision)
  - registry_indicator_embeddings -> 0003 (existing contract DDL)
  - scopes                        -> 0004 (existing, shape-equal)
  - the 12 tables below           -> this revision

Every statement is guarded (``IF NOT EXISTS``), so an already-bootstrapped
production database migrates as a verified no-op (the spec's adoption
scenario) while a fresh database builds the complete schema from the chain
alone. The DDL is NOT autogenerate output: it was derived from the live
authoritative shapes (``\d+`` export, reports/live-schema-dump.txt) and
reconciled column-by-column against the models in reports/adoption-diff.md —
production is the authority, models were aligned to match, and nothing here
alters an existing production object (no index production lacks is created,
no constraint is renamed or dropped in place).

Two deliberate non-adoptions: ``crawl_sources``'s ``trg_catch_nuller``
trigger (autogen-invisible, dispatcher-owned) and the ``spider_tests`` table
(an out-of-band dependent of ``manifests`` that was never in the family).
PostgreSQL-only like the rest of the chain — SQLite test consumers build
from the models with ``create_all``. Downgrade drops the twelve tables in
reverse dependency order and no further: it does not touch registry_tables
(their lifecycle lives in 0003/0004), and it intentionally fails rather
than cascades where out-of-band dependents (spider_tests) still reference
``manifests`` — dropping live control-plane history is not a supported move.
"""
from __future__ import annotations

from alembic import op

revision = "0006_control_plane_adoption"
down_revision = "0005_concept_coverage"
branch_labels = None
depends_on = None

# Creation order respects FK dependencies:
# crawl_sites -> (crawl_sources, pending_runs); crawl_runs -> crawl_items;
# crawl_identities -> (crawl_identity_events, crawl_login_stations);
# discoveries -> candidates -> analyses -> manifests.
_DDL = (
    """CREATE TABLE IF NOT EXISTS crawl_sites (
  id TEXT PRIMARY KEY,
  description TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL DEFAULT 'docker',
  enabled BOOLEAN NOT NULL DEFAULT true,
  last_seen_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
)""",
    """CREATE TABLE IF NOT EXISTS crawl_sources (
  source TEXT PRIMARY KEY,
  site TEXT REFERENCES crawl_sites(id),
  schedule TEXT,
  enabled BOOLEAN NOT NULL DEFAULT true,
  last_commit TEXT,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  auth_profile TEXT
)""",
    """CREATE TABLE IF NOT EXISTS crawl_runs (
  id BIGSERIAL PRIMARY KEY,
  source TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'runtime',
  status TEXT NOT NULL,
  started_at TIMESTAMPTZ NOT NULL,
  finished_at TIMESTAMPTZ,
  rows_written INTEGER NOT NULL DEFAULT 0,
  error_head TEXT,
  commit_sha TEXT,
  image_tag TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  cancel_requested TIMESTAMPTZ,
  pending_run_id BIGINT,
  identity_alias TEXT
)""",
    "CREATE INDEX IF NOT EXISTS crawl_runs_source_idx "
    "ON crawl_runs (source, created_at DESC)",
    """CREATE TABLE IF NOT EXISTS crawl_items (
  run_id BIGINT NOT NULL REFERENCES crawl_runs(id) ON DELETE CASCADE,
  idx INTEGER NOT NULL,
  payload JSONB NOT NULL,
  CONSTRAINT crawl_items_pkey PRIMARY KEY (run_id, idx)
)""",
    """CREATE TABLE IF NOT EXISTS crawl_identities (
  id BIGSERIAL PRIMARY KEY,
  source TEXT NOT NULL,
  account_alias TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'login_required',
  automation TEXT NOT NULL DEFAULT 'assisted',
  egress_ref TEXT,
  credentials_secret_ref TEXT,
  session_ref TEXT,
  lease_owner TEXT,
  lease_token TEXT,
  lease_expires_at TIMESTAMPTZ,
  last_login_at TIMESTAMPTZ,
  last_probe_at TIMESTAMPTZ,
  last_success_at TIMESTAMPTZ,
  consecutive_zero_runs INTEGER NOT NULL DEFAULT 0,
  failure_count INTEGER NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT crawl_identities_source_account_alias_key
    UNIQUE (source, account_alias),
  CONSTRAINT crawl_identities_status_check
    CHECK (status IN ('login_required','active','cooldown','banned','retired')),
  CONSTRAINT crawl_identities_automation_check
    CHECK (automation IN ('auto','assisted'))
)""",
    "CREATE INDEX IF NOT EXISTS crawl_identities_pool_idx "
    "ON crawl_identities (source, status)",
    "CREATE INDEX IF NOT EXISTS crawl_identities_lease_idx "
    "ON crawl_identities (lease_expires_at) WHERE lease_token IS NOT NULL",
    """CREATE TABLE IF NOT EXISTS crawl_identity_events (
  id BIGSERIAL PRIMARY KEY,
  identity_id BIGINT NOT NULL
    REFERENCES crawl_identities(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  detail TEXT,
  lease_token TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT crawl_identity_events_kind_check
    CHECK (kind IN ('login','probe','small_batch','lease_acquired',
                    'lease_released','lease_expired','auth_failed',
                    'suspect_yield','banned','note'))
)""",
    "CREATE INDEX IF NOT EXISTS crawl_identity_events_identity_idx "
    "ON crawl_identity_events (identity_id, created_at DESC)",
    """CREATE TABLE IF NOT EXISTS crawl_login_stations (
  id BIGSERIAL PRIMARY KEY,
  identity_id BIGINT NOT NULL
    REFERENCES crawl_identities(id) ON DELETE CASCADE,
  source TEXT NOT NULL,
  account_alias TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'launching',
  proxy_url TEXT,
  note TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  deadline_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ,
  CONSTRAINT crawl_login_stations_status_check
    CHECK (status IN ('launching','waiting_operator','completed','failed',
                      'timeout','reclaimed'))
)""",
    """CREATE TABLE IF NOT EXISTS discoveries (
  id SERIAL PRIMARY KEY,
  query TEXT NOT NULL,
  query_type VARCHAR(16) NOT NULL,
  status VARCHAR(32) NOT NULL,
  created_at TIMESTAMP,
  updated_at TIMESTAMP
)""",
    """CREATE TABLE IF NOT EXISTS candidates (
  id SERIAL PRIMARY KEY,
  discovery_id INTEGER NOT NULL REFERENCES discoveries(id),
  url TEXT NOT NULL,
  title TEXT,
  description TEXT,
  estimated_data_type VARCHAR(64),
  score INTEGER,
  coverage_status VARCHAR(32),
  coverage_details TEXT,
  adjusted_score INTEGER,
  created_at TIMESTAMP
)""",
    """CREATE TABLE IF NOT EXISTS analyses (
  id SERIAL PRIMARY KEY,
  candidate_id INTEGER NOT NULL REFERENCES candidates(id),
  discovery_id INTEGER NOT NULL REFERENCES discoveries(id),
  page_url TEXT NOT NULL,
  page_title TEXT,
  dom_snapshot TEXT,
  api_endpoints TEXT,
  data_tables TEXT,
  download_links TEXT,
  forms TEXT,
  raw_analysis TEXT,
  status VARCHAR(32) NOT NULL,
  created_at TIMESTAMP
)""",
    """CREATE TABLE IF NOT EXISTS manifests (
  id SERIAL PRIMARY KEY,
  discovery_id INTEGER NOT NULL REFERENCES discoveries(id),
  analysis_id INTEGER REFERENCES analyses(id),
  manifest_yaml TEXT NOT NULL,
  source_name VARCHAR(128),
  model_used VARCHAR(128),
  status VARCHAR(32) NOT NULL,
  validation_error TEXT,
  created_at TIMESTAMP,
  updated_at TIMESTAMP
)""",
    """CREATE TABLE IF NOT EXISTS pending_runs (
  id BIGSERIAL PRIMARY KEY,
  source TEXT NOT NULL,
  site TEXT NOT NULL DEFAULT 'tencent' REFERENCES crawl_sites(id),
  params JSONB NOT NULL DEFAULT '{}',
  requested_by TEXT NOT NULL DEFAULT 'console',
  status TEXT NOT NULL DEFAULT 'pending',
  claimed_by TEXT,
  claimed_at TIMESTAMPTZ,
  lease_until TIMESTAMPTZ,
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 2,
  run_id BIGINT,
  error_head TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ,
  CONSTRAINT pending_runs_status_check
    CHECK (status IN ('pending','claimed','done','failed','cancelled'))
)""",
    "CREATE INDEX IF NOT EXISTS pending_runs_site_idx "
    "ON pending_runs (site, status, created_at)",
)

# Reverse of _DDL's dependency order.
_DOWNGRADE_DROPS = (
    "manifests",
    "analyses",
    "candidates",
    "discoveries",
    "crawl_login_stations",
    "crawl_identity_events",
    "crawl_identities",
    "pending_runs",
    "crawl_items",
    "crawl_runs",
    "crawl_sources",
    "crawl_sites",
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
    for table in _DOWNGRADE_DROPS:
        bind.exec_driver_sql(f"DROP TABLE IF EXISTS {table}")
