# Make the census refresh survive a big store and a dead shard

## Why

The reconciler refreshes `data_census` on every tick when the newest sample is
older than 24h. It has been silently failing since **2026-11-03 18:07 UTC** —
`sampled_at` never moved and every downstream reader (`data_stats`, the panel's
staleness marker, coverage planning) has been reading five-day-old numbers.
Because a census failure is deliberately downgraded to `WARNING census refresh
skipped: …` (spec: a census problem must never break crawling), nothing else in
the tick looked wrong.

Two independent defects stacked up, and the second was invisible behind the first:

1. **`integer` counters.** `data_census.approx_rows` / `total_size_bytes` were
   int4 (max 2,147,483,647). The local store passed that on 2026-11-08
   (`total_size_bytes = 2,873,810,944`), so the local row's update failed with
   `(psycopg2.errors.NumericValueOutOfRange) integer out of range`.
2. **A failed probe poisons the whole transaction.** `refresh_census` catches a
   probe exception and records it on that store's row, but in PostgreSQL a
   failed statement aborts the enclosing transaction. The `shard_xinru3` dblink
   probe raising therefore left every subsequent statement (and the final
   `commit`) failing with `InFailedSqlTransaction: current transaction is
   aborted` — discarding the whole census, **including the local row that had
   just been computed successfully**. One dead shard took down the entire
   refresh, so fixing (1) alone only changed the error text.

## What changes

- `DataCensus.approx_rows` / `total_size_bytes` become `BigInteger`; Alembic
  revision `0017_census_bigint` widens both columns in place (int4 → int8 is
  not a table rewrite).
- Every leg of `refresh_census` (local count/size, shard-server listing, each
  shard probe) runs inside its own `session.begin_nested()` SAVEPOINT, so a
  failing leg rolls back only itself and the remaining stores still commit.
- Tests: model columns are 64-bit; a row above the int4 range round-trips; one
  SAVEPOINT per leg is taken.

## Impact

- Affected specs: `data-census`.
- Affected code: `fd_open_data_mcp/models.py`,
  `fd_open_data_mcp/visibility/census.py`, Alembic `0017_census_bigint`,
  `tests/test_census.py`, `tests/test_census_bigint.py`.
- Runtime: no API change. The production database was widened in place on
  2026-11-08 and stamped to `0017_census_bigint`; the Python-side savepoint fix
  ships in the next patch release and needs an image roll to take effect.
- Not in scope: the dead `shard_xinru3` dblink server itself (its probe keeps
  recording an error on its row, which is the designed behavior).
