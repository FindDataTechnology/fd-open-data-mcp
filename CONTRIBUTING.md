# Contributing

Thanks for your interest in improving fd-open-data-mcp — the open-data semantic
supply engine. This project is MIT-licensed and developed in the open.

## Before you start

- Read the [README](README.md) and run the one-command quickstart
  (`docker compose -f docker-compose.dev.yaml up`) to see the stack end to end.
- The system is spec-driven: behavior lives in `openspec/specs/`, proposed
  changes in `openspec/changes/`. Non-trivial features and fixes go through a
  change proposal before code. See [AGENTS.md](../AGENTS.md) for the workflow.

## Development setup

```bash
git clone <your-fork>
cd fd-open-data-mcp
uv sync --extra data        # full datasource support
uv sync --extra dev         # + test tooling (if present)
uv run pytest -q            # the suite runs against isolated SQLite; a few
                            # integration tests use a live redis-server and
                            # skip automatically when it is absent
```

The default database is SQLite (`fd_open_data_mcp/metadata/daas.db`); set
`FD_OPEN_DATA_MCP_DATABASE_URL` to point at PostgreSQL. Schema changes are
Alembic migrations under `alembic/versions/` — add one, never hand-edit the
schema gate.

## Making a change

1. Open a change proposal (`openspec/changes/<name>/`) describing the why, the
   requirement deltas, and the tasks.
2. Implement the tasks, keeping each `### Requirement` scenario testable.
3. Run `uv run pytest` and `openspec validate <name> --strict`.
4. Open a pull request: describe what changed and link the proposal. CI runs
   the test suite; the conformance gate must pass before merge.

## Conventions

- **Fail loud, never silent.** An empty result must carry a reason
  (`quota` / `unreachable` / `not_indexed` / `data_not_ready`), matching the
  `concept-fetch` and `analysis-plane` contracts.
- **Idempotency is the natural key.** Writes upsert on
  `(concept, entity, date, granularity, source)`; never add a side dedup ledger.
- **The rollup is the read path.** Aggregates read `concept_coverage` /
  `concept_source_stats`, never a full scan of `semantic_observations`.
- Keep datasource providers pluggable (entry-points / manifests); adding a
  datasource must not require editing the shared engine.

## Reporting issues

Use the issue tracker with a minimal reproduction. For security issues, see
[SECURITY.md](SECURITY.md) — do not open a public issue.
