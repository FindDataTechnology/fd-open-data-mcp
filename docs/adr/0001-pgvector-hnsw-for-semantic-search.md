# ADR 0001: pgvector + HNSW as the vector engine for semantic search

- **Status**: Accepted (2026-09-29, user-approved during `mcp-search-engine-overhaul` apply)
- **Context**: OpenSpec change `mcp-search-engine-overhaul` (design D4)
- **Deciders**: user (infra touch sign-off), applying agent

## Context

Concept (~450 active rows) and entity (~5.3k rows) embeddings were stored as
JSONB/TEXT and scanned in full on every semantic query, deserializing and
dot-producting every vector in Python. The unified indicator registry
(57,553 entries) was about to enter the corpus, which makes the per-call
full-scan cost and memory footprint structural rather than incidental.

## Decision

Move embedding storage to native `vector(384)` columns with HNSW indexes
(`vector_cosine_ops`) in the authoritative PostgreSQL (`fd_open_data`,
xinru-master), and serve similarity queries inside the database via the
`<=>` indexed operator.

This required swapping the `fd-postgres` container image from
`postgres:16` to `pgvector/pgvector:pg16-trixie` — the only infrastructure
touch of the change, explicitly approved by the user on 2026-09-29
(data dir is a bind mount; same PG major; recreate is reversible;
the `-trixie` variant was chosen to match the prior container's glibc 2.41
and avoid a collation-version reindex).

## Alternatives considered

1. **Permanent in-process numpy matrix** (transitional design D6): rejected
   as the end state — a 57k × 384 float32 matrix (~88 MB) plus resident
   torch runtime would squeeze the 1 Gi pod limit and the corpus could not
   grow further; kept only as the migration-window backend
   (`FD_MCP_VECTOR_BACKEND=matrix`).
2. **apt-installing the extension into the running container**: rejected —
   the binary lives only in the container layer and silently disappears on
   any future recreate, leaving the catalog referencing a missing extension.
3. **Staying on JSON + Python scan**: rejected — O(corpus) work per query
   and full-table deserialization, incompatible with a 57k+ corpus and the
   spec's "query does not pull the full table" requirement.

## Consequences

- **Migration path** (each step independently reversible): add vector
  columns → backfill from JSON → dual-read verification job (sampled
  ranking consistency threshold) → flag-flip reads to `pgvector`
  (`FD_MCP_VECTOR_BACKEND`) → drop JSON columns after an observation
  window.
- **Rollback**: set `FD_MCP_VECTOR_BACKEND=json` (or `matrix`); the JSON
  columns are kept until the observation window closes.
- **Model coupling**: the columns are typed `vector(384)` to match
  all-MiniLM-L6-v2 (`FD_MCP_EMBEDDING_MODEL`); switching embedding models
  later means new-dimension columns and a re-embed, not an in-place ALTER.
- **Infrastructure**: every environment that runs migrations on the
  authoritative PG needs the pgvector-enabled image; SQLite dev/test
  databases never see the vector columns (dialect-guarded migration).
