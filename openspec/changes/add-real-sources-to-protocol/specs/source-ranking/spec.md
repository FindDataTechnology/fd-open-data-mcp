## MODIFIED Requirements

### Requirement: Per-(source × concept) multi-dimensional ranking

The system SHALL rank each `(source, concept)` pair on three dimensions: `quality` (authoritativeness, revision behavior, coverage), `accessibility` (latency, rate-limit, auth/cost, reliability), and `freshness-fit` (how current the source is for the concept's frequency). When a function declares `real_sources`, the system SHALL also track per-`real_source` rankings (e.g., `(eastmoney, concept)`, `(tencent, concept)`). A single composite score SHALL be derivable per request.

#### Scenario: Authoritative source outranks scraped source for the same concept

- **WHEN** both `worldbank` and a scraped source provide `gdp.current` for a country
- **THEN** `worldbank` has a higher `quality` score for that concept
- **AND** the composite rank favors `worldbank` for historical GDP requests

#### Scenario: Real source ranking for functions with real_sources

- **WHEN** a function declares `real_sources: [{name: eastmoney, priority: 0}, {name: tencent, priority: 1}]`
- **AND** eastmoney has higher accessibility than tencent for the concept
- **THEN** the system SHALL rank `(eastmoney, concept)` higher than `(tencent, concept)`
- **AND** dispatch SHALL prefer eastmoney over tencent

### Requirement: Accessibility self-tunes from fetch outcomes

The system SHALL adjust a source's `accessibility` score from recorded fetch outcomes: failures (429, timeout, 5xx) and high latency lower it; successes raise it. When a function declares `real_sources`, the system SHALL adjust per-`real_source` accessibility (e.g., eastmoney's accessibility is adjusted independently from tencent's). The adjustment SHALL be bounded so a single failure cannot permanently remove a source.

#### Scenario: Repeated 429s demote a source

- **WHEN** `akshare` returns 429 on three consecutive fetches for a concept
- **THEN** its `accessibility` score for that concept decreases
- **AND** a lower-accessibility alternative is selected on the next dispatch

#### Scenario: Real source accessibility self-tunes

- **WHEN** a function declares `real_sources: [{name: eastmoney}, {name: tencent}]`
- **AND** eastmoney returns 503 on three consecutive fetches
- **THEN** eastmoney's `accessibility` score decreases
- **AND** tencent's `accessibility` score is unchanged
- **AND** dispatch SHALL prefer tencent over eastmoney on the next fetch

## ADDED Requirements

### Requirement: Per-real-source fetch_log tracking
The `fetch_log` table SHALL record the `real_source` used for each fetch (in addition to the library-level `source`). When a function declares `real_sources`, the `fetch_log` row SHALL include the specific `real_source` that was actually called (e.g., "eastmoney"). When a function does not declare `real_sources`, the `real_source` field SHALL be NULL (backward compatibility).

#### Scenario: fetch_log records real_source for function with real_sources
- **WHEN** a function declares `real_sources: [{name: eastmoney}, {name: tencent}]`
- **AND** the fetch calls eastmoney
- **THEN** the `fetch_log` row SHALL have `real_source = "eastmoney"`
- **AND** `source = "akshare"` (library-level)

#### Scenario: fetch_log records NULL real_source for function without real_sources
- **WHEN** a function does not declare `real_sources`
- **AND** the fetch calls akshare
- **THEN** the `fetch_log` row SHALL have `real_source = NULL`
- **AND** `source = "akshare"` (library-level)

### Requirement: Failover based on real_source priority
When a real source is banned (circuit OPEN), the system SHALL automatically failover to the next priority real source (if declared in the function's `real_sources`). The failover SHALL respect the priority order (0 = primary, 1+ = failover). If all real sources are banned, the system SHALL raise `SourceUnavailable`.

#### Scenario: Failover from eastmoney to tencent
- **WHEN** a function declares `real_sources: [{name: eastmoney, priority: 0}, {name: tencent, priority: 1}]`
- **AND** eastmoney's circuit is OPEN (banned)
- **THEN** the system SHALL failover to tencent
- **AND** the fetch SHALL call tencent's endpoint
- **AND** the `fetch_log` row SHALL have `real_source = "tencent"`

#### Scenario: All real sources banned
- **WHEN** a function declares `real_sources: [{name: eastmoney}, {name: tencent}]`
- **AND** both eastmoney's and tencent's circuits are OPEN
- **THEN** the system SHALL raise `SourceUnavailable`
- **AND** the caller SHALL handle the error (e.g., skip the concept, alert the operator)
