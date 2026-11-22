## 1. Investigation

- [x] 1.1 Inspect `seed_entity_identifiers` (and the registration path that wrote stock `entity_source_identifiers`) to determine what produced `02211`/`02219` for 中国银行/平安银行; record the finding in `design.md` Open Questions.
- [x] 1.2 Verify `astock_daily` schema (column names) and coverage: does it contain 中国银行 (`601988`) and the 2934 zero-observation stocks? Record column mapping to System-B concepts (`price.close`, `price.open`, …).

## 2. Schema migration

- [x] 2.1 Add Alembic migration: `ALTER TABLE concepts ADD COLUMN deprecated BOOLEAN NOT NULL DEFAULT false`.
- [x] 2.2 Add composite index migration on `semantic_observations(entity_id, concept_id, date)` if not present; verify via `EXPLAIN` on a sample multi-date `read()` query.

## 3. Stock source-identifier repair

- [x] 3.1 Add `repair-stock-identifiers` CLI subcommand (dry-run default, `--apply` writes) that reports stock entities whose `akshare` identifier != 6-digit `code` and derives canonical `akshare` (code) and `yfinance` (`.SS`/`.SZ` suffix by code prefix).
- [x] 3.2 Fix `seed_entity_identifiers` to derive `akshare`/`yfinance` from `entity.code` (per `stock-source-identity` spec) so future registrations are correct.
- [x] 3.3 Run repair `--apply` on all 5212 stock entities; verify `resolve_entity(stock, 3112, akshare) == "601988"` and `resolve_entity(stock, 3117, yfinance) == "000001.SZ"`; confirm `cn-report` rows unchanged.

## 4. Concept deprecation

- [x] 4.1 Add `deprecated` field to the `Concept` SQLAlchemy model (`models.py`) and ensure `list_concepts`/`get_entity`-style reads expose it.
- [x] 4.2 Mark the 120 `entity_type='symbol'` concepts in stock financial categories (`股票行情`, `利润表`, `资产负债表`, `现金流量表`, `财务指标`) as `deprecated=true` via an idempotent migration/script; re-run changes no rows.
- [x] 4.3 Make `read()`, `fetch()`, `rank_sources()`, `plan_crawl()` reject deprecated `concept_id` with an error naming the canonical replacement (per `concept-canonicalization` "Deprecated concepts excluded from dispatch").
- [x] 4.4 Add a code-to-canonical alias record (or doc) mapping `PRICE_CLOSE`->`price.close`, `PS_REVENUE`->`financials.revenue`, etc., for migration reference.

## 5. Discovery gating

- [x] 5.1 In `semantic_search.py`: exclude `deprecated=true` concepts; rank concepts with ≥1 binding ahead of zero-binding concepts within a 0.05 similarity tolerance.
- [x] 5.2 In `ai_search.py`: default to excluding zero-binding concepts; add `include_unbound` parameter (default false) for exploratory mode.
- [x] 5.3 Verify `ai_search("中国银行收盘价")` surfaces `price.close` (id 234), not `PRICE_CLOSE` (id 169).

## 6. read/fetch resilience

- [x] 6.1 Chunk multi-date `read()` queries into bounded date windows and aggregate; ensure no single query scans an unbounded range of `semantic_observations`.
- [x] 6.2 Ensure the sentence-transformer model loads once at module level (lazy singleton), not per `read()`/`ai_search()` call.
- [x] 6.3 Verify `read(concept_id=234, stock, 3112, [12 dates])` no longer drops the MCP connection.

## 7. Ingestion backfill

- [x] 7.1 If `astock_daily` covers the 2934 missing stocks, bulk-migrate relevant rows into `semantic_observations` keyed by `stock` entities + System-B concepts, using `ON CONFLICT (entity_id, concept_id, date) DO NOTHING`.
- [ ] 7.2 For stocks absent from `astock_daily`, live-`fetch()` via ranked dispatch (now that identifiers are correct) with proxy pool + circuit breaker + rate limiting. — **DEFERRED**: sources (akshare/eastmoney/yfinance) unreachable from this host (`fetch` returns "no source succeeded"); run on the cluster where sources are reachable. The fetch path is wired and identifiers are corrected.
- [x] 7.3 Verify `semantic_observations` row count for entity 3112 (中国银行) and entity 3117 (平安银行) is non-zero for `price.close`.

## 8. End-to-end verification

- [x] 8.1 `read(concept_id=234, entity_type='stock', entity_id=3112, dates=[recent quarter-ends])` returns 中国银行 closing prices.
- [ ] 8.2 `fetch(concept_id=234, entity_type='stock', entity_id=3112, date=<recent>)` dispatches to akshare and populates cache. — **DEFERRED**: same source-unreachable blocker as 7.2; cache is populated via the `astock_daily` migrate path instead.
- [x] 8.3 `ai_search("中国银行最近三年数据", include_values=true)` returns retrievable stock concepts with non-empty values.
- [x] 8.4 Run `openspec validate fix-stock-semantic-retrieval`; run package smoke tests (`pytest`). - openspec validate passes. pytest: 109 passed, 52 errors + 9 failures, all from a **pre-existing** SQLite/JSONB test-infra issue (`entities.metadata_json` is `JSONB`, unrenderable on SQLite; Entity model untouched by this change). End-to-end behavior verified against the live Postgres instead.
