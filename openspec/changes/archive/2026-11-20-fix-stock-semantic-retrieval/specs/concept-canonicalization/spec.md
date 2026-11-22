## ADDED Requirements

### Requirement: Concept deprecation flag
The `concepts` table SHALL carry a `deprecated` boolean (default false). A deprecated concept is retained for audit but excluded from discovery and dispatch.

#### Scenario: Deprecation is additive and reversible
- **WHEN** a migration adds the `deprecated` column
- **THEN** all existing concepts SHALL default to `deprecated=false`
- **AND** setting `deprecated=true` on a concept SHALL NOT delete its row, bindings, or embeddings

### Requirement: One canonical concept per metric
The system SHALL designate exactly one canonical concept for each (category, metric) pair. Where duplicate concepts exist for the same metric across entity types, the concept matching the populated entity type is canonical and the others are deprecated.

#### Scenario: Stock price-close has one canonical concept
- **WHEN** the stock-domain price-close metric is considered
- **THEN** the `entity_type='stock'` concept `price.close` (id 234) SHALL be canonical
- **AND** the `entity_type='symbol'` concept `PRICE_CLOSE` (id 169) SHALL be marked `deprecated=true`

#### Scenario: Non-stock symbol concepts remain canonical
- **WHEN** a `symbol`-typed concept belongs to a non-stock domain (futures `FUT_*`, crypto `CRYPTO_*`, ETF `ETF_*`, index `IDX_*`, technical indicator `TA_*`)
- **THEN** the concept SHALL NOT be deprecated
- **AND** it SHALL remain the canonical concept for its domain

### Requirement: Deprecation targets stock-domain symbol duplicates
The deprecation SHALL apply to `entity_type='symbol'` concepts whose `category` is one of the stock financial categories (`股票行情`, `利润表`, `资产负债表`, `现金流量表`, `财务指标`), because canonical `stock`-typed twins exist for the same metrics.

#### Scenario: Bulk deprecation is idempotent
- **WHEN** the deprecation step is run
- **THEN** every `symbol`-typed concept in a stock financial category SHALL be set `deprecated=true`
- **AND** running the step again SHALL change no rows

### Requirement: Deprecated concepts excluded from dispatch
`read()`, `fetch()`, `rank_sources()`, and `plan_crawl()` SHALL NOT use deprecated concepts as dispatch targets and SHALL return an explicit error when a deprecated concept_id is requested.

#### Scenario: Read refuses deprecated concept
- **WHEN** `read()` is called with a `concept_id` whose `deprecated=true`
- **THEN** the system SHALL return an error naming the canonical replacement concept
- **AND** SHALL NOT query `semantic_observations`
