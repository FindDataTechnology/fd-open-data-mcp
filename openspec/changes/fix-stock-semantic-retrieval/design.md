## Context

The semantic layer in `fd-open-data-mcp` exposes stock data through two parallel concept systems, only one of which is wired:

- **System B (canonical, wired)** - 27 `entity_type=stock` concepts with dotted-lowercase codes (`price.close` id 234, `financials.revenue` id 239, …). 117 bindings on `price.close`, 18.5M observations across 6275 entities, 44M stock observations total. Matches the 5212 `stock` entities.
- **System A (ghost, unwired)** - 120 `entity_type=symbol` concepts with UPPER_SNAKE codes (`PRICE_CLOSE` id 169, `PS_REVENUE` id 71, `FIN_ROE` id 199, …). Zero bindings, zero observations. They exist only as concept + embedding rows.

Discovery (`ai_search`, `semantic_search`, `semantic_search_unified`) vector-searches the concept embeddings and surfaces System A ghosts (they have embeddings, no bindings) -> callers hit dead ends. `read()` then enforces `concept.entity_type == entity_type` (correct) and refuses, or `fetch()` finds no bindings -> "no source succeeded".

A second, independent blocker: `entity_source_identifiers` stores wrong akshare/yfinance identifiers for stocks - 中国银行 (code `601988`, entity 3112) resolves to akshare `02211`; 平安银行 (code `000001`, entity 3117) to `02219`. These are not tickers and break `fetch()` ranked dispatch even where bindings exist. The `cn-report` identifier is correct (`601988`).

Finally, of 5212 stock entities, only 2278 have observations; 中国银行 and 平安银行 have zero. A legacy `astock_daily` table exists in the same Postgres and is a candidate bulk-ingest source (column schema to confirm during implementation).

`semantic_observations` is 96M rows; a multi-date `read()` against it dropped the MCP connection ("Connection closed").

## Goals / Non-Goals

**Goals:**
- `read(concept_id=234, entity_type="stock", entity_id=3112, dates=[…])` returns 中国银行's closing-price series.
- `fetch()` for a stock concept dispatches to the correct akshare function via the correct ticker identifier.
- Discovery no longer surfaces unbound deprecated concepts as primary results.
- Stock entities resolve to correct per-source identifiers (akshare = 6-digit ticker, yfinance = exchange-suffixed).
- `read()` does not crash the MCP connection on large tables.

**Non-Goals:**
- Re-crawling or re-fetching data for stocks that already have observations (2934-stock backfill is in scope; refreshing existing data is not).
- Touching non-stock `symbol` concepts (futures/crypto/ETF/index/technical-indicator) - they remain canonical for their domains.
- Changing the `read`/`fetch` entity_type validation contract (it is correct; the bug was discovery leading to the wrong concept type).
- Migrating the legacy `astock_daily` table away - it stays as a bulk-ingest source if used.

## Decisions

### D1. Source-identifier repair: derive akshare id from `entity.code`, not a secondary lookup
Stock `entity.code` IS the 6-digit ticker (`601988`, `000001`). The akshare identifier for an A-share is the bare 6-digit code; yfinance is exchange-suffixed (`601988.SS`, `000001.SZ` - `SS` for Shanghai 60xxxx, `SZ` for Shenzhen 00/30xxxx).

- Re-seed by deriving directly from `entity.code` + exchange inference from the code prefix. No external table needed.
- Preserve the existing correct `cn-report` identifiers; only rewrite `akshare` and `yfinance` rows for `stock` entities.
- **Alternative considered**: re-run the original `seed_entity_identifiers` function. Rejected until the function is audited - if it produced `02211`/`02219`, re-running may reproduce the bug. The function must be inspected and fixed first (see Open Questions).
- Add a `validate`/`repair` CLI subcommand that reports drift (stock entities whose akshare id != code) and fixes them idempotently.

### D2. Concept duality: deprecate, do not delete
Add a `deprecated` boolean column to `concepts` (default false). Mark the 120 `symbol`-typed stock-domain concepts deprecated. Keep the rows (embeddings, audit trail); exclude deprecated concepts from discovery and dispatch.

- **Identification of stock-domain symbol concepts**: `entity_type='symbol'` AND `category` IN (`股票行情`, `利润表`, `资产负债表`, `现金流量表`, `财务指标`). The canonical `stock`-typed twins (`price.close` etc.) cover the same metrics.
- **Alternative considered**: hard-delete. Rejected - loses the audit trail and is needlessly BREAKING.
- **Alternative considered**: repoint/merge System A into System B. Rejected - System A has no bindings/observations to repoint; deprecation achieves the same discovery/dispatch exclusion with less risk.

### D3. Discovery gating: rank bound concepts ahead, exclude deprecated, flag-based unbound exclusion
- Deprecated concepts are always excluded from `semantic_search`/`semantic_search_entities`/`semantic_search_unified`/`ai_search` results.
- Among non-deprecated results, concepts with ≥1 binding are ranked above zero-binding concepts.
- `ai_search` defaults to excluding zero-binding concepts (the "give me data" intent); `semantic_search` keeps them behind `exclude_unbound=false` for exploratory use.
- **Alternative considered**: hard-exclude all unbound concepts everywhere. Rejected - weakens exploratory discovery of newly-registered concepts not yet bound.

### D4. read/fetch resilience: date-range chunking + index guard, no behavior change
- Ensure a composite index exists on `semantic_observations(entity_id, concept_id, date)` (verify; add migration if missing).
- `read()` with many dates chunks the query into bounded windows and aggregates, so no single query scans unbounded ranges of the 96M-row table.
- The "Connection closed" is most likely a long-held connection / OOM on a large result set; chunking + server-side cursor bounds it.
- **Alternative considered**: materialize per-entity daily series into a separate table. Rejected as out of scope; chunking is sufficient for retrieval.

### D5. Ingestion backfill: prefer legacy `astock_daily` bulk load, fall back to live `fetch`
- 2934 stocks with zero observations is too many to live-fetch (eastmoney ban risk, even with the proxy pool).
- If `astock_daily` contains these stocks (verify column schema + coverage), bulk-migrate the relevant rows into `semantic_observations` keyed by `stock` entities + System B concepts. Reuse the existing migrate path (`scraw-fd-open-data-mcp` migrate mode) rather than a new pipeline.
- Only fall back to live `fetch()` per-stock for tickers absent from `astock_daily`.
- **Alternative considered**: live-fetch all 2934 via `run_schedule`. Rejected - high ban risk, slow, and redundant if `astock_daily` already holds the history.

## Risks / Trade-offs

- [Re-seed overwrites a correct mapping] -> Mitigation: only touch `akshare`/`yfinance` rows for `entity_type='stock'`; never touch `cn-report`; dry-run reports the diff before applying.
- [Deprecating a concept that something references] -> Mitigation: none of the 120 have bindings or observations; keep rows for audit. Document the code-to-canonical mapping (`PRICE_CLOSE` -> `price.close`) in the deprecation record.
- [`astock_daily` schema/coverage differs from assumed] -> Mitigation: D5 makes `astock_daily` use conditional on verification; live `fetch()` is the fallback, which works once D1 fixes identifiers.
- [Backfill from `astock_daily` writes duplicate observations] -> Mitigation: insert with `ON CONFLICT (entity_id, concept_id, date) DO NOTHING` (Postgres) / idempotent upsert.
- [`read()` crash root cause is actually the embedding model load, not the query] -> Mitigation: D4 also ensures the sentence-transformer model is loaded lazily once (module-level cache), not per call.
- [Re-seeding 5212 stocks is slow / rate-limited] -> Mitigation: pure local DB writes (no network), bounded by a single batch transaction.

## Migration Plan

1. **Audit `seed_entity_identifiers`** - confirm what produced `02211`/`02219`; fix the function so re-seeding derives from `entity.code`.
2. **Add `concepts.deprecated` column** - additive migration, default false.
3. **Re-seed stock source identifiers** - dry-run diff, then batch-upsert correct akshare/yfinance rows for all 5212 stock entities. Verify `resolve_entity(stock, 3112, akshare) == "601988"`.
4. **Mark 120 `symbol` stock concepts deprecated** - idempotent `UPDATE concepts SET deprecated=true WHERE entity_type='symbol' AND category IN (...)`.
5. **Ship discovery gating + read chunking** - code change in `semantic_search.py`, `ai_search.py`, `server.py` read/fetch.
6. **Backfill observations** - verify `astock_daily` coverage; bulk-migrate 2934 missing stocks into `semantic_observations`; live-`fetch` any remainder.
7. **Verify** - `read(concept_id=234, stock, 3112, [recent dates])` returns 中国银行 prices; `ai_search("中国银行股价")` surfaces `price.close` not `PRICE_CLOSE`.

Rollback: steps 2 (drop column), 4 (`SET deprecated=false`), 3 (restore prior identifiers from a pre-migration dump) are reversible; step 6 inserts are idempotent and can be deleted by `entity_id`/`concept_id`.

## Open Questions

- **RESOLVED (1.1)**: `seed_stock_identifiers` (resolver.py:130) is correct in intent - it sets `akshare=code` and `yfinance=_yfinance_symbol(...)`. It did **not** produce `02211`/`02219`; those came from a different importer (the values look like cn-report/fd-entities internal IDs written into the akshare/yfinance rows). Re-running `seed_stock_identifiers` overwrites them via `ON CONFLICT DO UPDATE`. **Latent bug found**: `_yfinance_symbol` checks the `exchange` string first; stock metadata is the ambiguous `"SSE/SZSE"` with no `market` field, so a Shanghai code (`601988`) wrongly matches the `"SZ" in ex` branch -> `.SZ`. Fix: derive the suffix from the code prefix per the `stock-source-identity` spec (`.SS` for `60xxxx`, `.SZ` for `00/30xxxx`), not from the ambiguous exchange string.
- **RESOLVED (1.2)**: `astock_daily` covers 5203/5212 stock symbols (中国银行: 4849 rows 2006-07-05..2026-07-10; 平安银行: 4746 rows). Columns: `symbol, trade_date, period, adjust, open, close, high, low, volume, amount, amplitude, pct_change, change, turnover`. D5 bulk-migrate is the ingestion path; live `fetch()` is only the fallback for the 9 stocks absent from `astock_daily`.
- Should the `symbol`->`stock` concept code mapping be recorded as a structured alias table (so external `PRICE_CLOSE` references auto-redirect to `price.close`), or is documenting it enough? Defer until a real caller exists.
