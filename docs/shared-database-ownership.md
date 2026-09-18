# Shared database ownership: `fd_open_data` on guangzhou-xinru

The canonical ontology database has **two owning services**. This document is
the table-ownership map and the rules both deploy steps must follow
(db-schema-lifecycle design D7).

## Ownership map

| Objects | Owner | Versioned by |
|---|---|---|
| The 27 baseline tables (`sources`, `functions`, `concepts`, `semantic_observations`, `crawl_policies`, `entity_embeddings`, …) | fd-open-data-mcp | `alembic_version` — chain rooted at `0001_schema_baseline`, shipped in the MCP image |
| `alembic_version` | fd-open-data-mcp | the Alembic chain itself |
| `source_provider_routes` | fd-proxy-service | fd-proxy-service's own runner (its own ledger if/when it adopts one — never this chain) |
| `proxies` | written by **both** services; **schema migrated by fd-open-data-mcp only** (the model-owning service) | fd-open-data-mcp |

History note: fd-proxy-service's `migrations/001/002` historically added
columns to `proxies` (`provider`, `max_concurrency`) and created
`source_provider_routes`. Those `proxies` columns are now modelled and part of
the MCP baseline; fd-proxy-service must not ALTER `proxies` again — request the
change through an MCP revision instead. (The `provider` TEXT-vs-VARCHAR(64)
divergence of 2026-08 is what this rule prevents.)

## Rules

1. **One migrator per service, only for its own tables.** A service never
   migrates another service's tables. fd-proxy-service keeps its
   `k8s/proxy-migrate-job.yaml` runner for its own objects.
2. **One ledger per owner.** fd-open-data-mcp versions with Alembic
   (`alembic_version`). If fd-proxy-service adopts a ledger later, it configures
   its own Alembic `version_table` name — both live in the shared database, and
   each service's startup gate reads only its own.
3. **Shared advisory lock serializes migrators.** The MCP migration stage takes
   session-level `pg_advisory_lock(hashtext('fd_mcp_schema_migrate'))` around
   `alembic upgrade head` (`fd_open_data_mcp/db/migrate_stage.py`, run as the
   `migrate-schema` initContainer). fd-proxy-service's runner must take the
   **same lock** before executing its SQL, so the two migrators can never race.
4. **Read-only consumers get the gate, not a migrator.** Batch/crawl Jobs that
   only read the database never run migrations; the MCP image's startup gate
   (`db >= required`, env `FD_OPEN_DATA_MCP_SCHEMA_GATE_BYPASS=1` to bypass
   loudly) applies to anything shipping the MCP code. A consumer that bundles
   this code in its own image (e.g. the scraw crawl workers) must also ship
   `alembic/` + `alembic.ini` in the image, or the gate fails closed with
   "the image does not ship the alembic scripts" — see both Dockerfiles for
   the COPY lines.
5. **Governed names come from fd-open-data-protocol.** Concept family/variable
   ids, entity types, relation types, units, qualifiers, event types and real
   source names are defined in its versioned vocabulary; models, migrations and
   seeds consume them (`scripts/check_vocabulary_conformance.py` enforces the
   DB side).

## Where the deploy steps look

- fd-open-data-mcp: the `migrate-schema` initContainer in
  `k8s/zihan/20-fd-open-data-mcp.yaml` (and any other cluster's copy of the
  manifest — e.g. fd-official-web's `:torch` deployment — which must gain the
  same initContainer).
- fd-proxy-service: header comment of `k8s/proxy-migrate-job.yaml` points here.
