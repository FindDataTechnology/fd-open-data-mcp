# Migrations archive — the retired SQL runbook series

This directory is the frozen `migrations/` series (2026-08-04 → 2026-09-18):
hand-written SQL change records applied manually against live databases,
each usually paired with a `.rollback.sql`. The series is retired by the
db-schema-lifecycle change — the Alembic chain
(`alembic/versions/`, rooted at `0001_schema_baseline`) is now the single
migration mechanism, and nothing here executes anymore.

The directory was relocated from `migrations/` so its numbering can no
longer be confused with `alembic/versions/` (two unrelated "006"s once
coexisted).

## Disposition of each file

| File | Fate |
|---|---|
| `001_add_real_sources_to_functions[.rollback].sql` | effect modelled → absorbed into the baseline (`0001_schema_baseline`) |
| `002_add_real_source_to_fetch_log.sql` | absorbed into the baseline |
| `003_add_concepts_deprecated[.rollback].sql` | absorbed into the baseline |
| `004_add_semantic_observations_entity_index[.rollback].sql` | absorbed into the baseline |
| `005_add_observation_granularity[.rollback].sql` | absorbed into the baseline |
| `006_add_policy_runs_origin_cancelled[.rollback].sql` | absorbed into the baseline |
| `007_multi_source_observations.sql` | **superseded** by revision `0002_sem_obs_source_aware_key` (the swap now runs as an Alembic revision under the same advisory lock this runbook used) |
| `007_multi_source_observations.rollback.sql` | kept as an **ops document only** — the lossy manual reverse (dedup per `source_rankings`, then rebuild the 5-column key). Not part of the mechanism; normal rollback is redeploying the previous image |

New schema changes never land here. They land as revisions in
`alembic/versions/`; `alembic upgrade --sql` produces a reviewable psql
runbook when one is wanted.
