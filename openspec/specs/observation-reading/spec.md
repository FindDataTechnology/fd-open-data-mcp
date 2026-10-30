# observation-reading Specification

## Purpose
Serving cached observations over MCP: point reads that resolve by period rather than exact date, a range read for whole series, and cache-first semantics for bulk reads — so the millions of stored rows are servable without a live upstream fetch.

## Requirements

### Requirement: Period-aware cache hit on read
When `read` is asked for a date that falls inside a fully-elapsed period of the concept's frequency (e.g. any day of 2020 for a yearly concept) and the cache holds that period's observation, the read SHALL return the cached observation instead of treating the date as a miss. Exact-date matches and current-period (possibly stale) semantics SHALL behave as before; source-pinned reads SHALL keep selecting that source's row.

#### Scenario: Yearly concept hit through an off-peak date
- **WHEN** a client reads a yearly concept for entity E at date `2020-07-01` and the cache holds E's `2020-01-01` (or any single date within 2020) observation
- **THEN** the cached value is returned with `from_cache: true` and no upstream dispatch occurs

#### Scenario: Current period still freshness-checked
- **WHEN** the requested date falls in the current (not fully elapsed) period and the cached row is stale per the frequency TTL
- **THEN** the read dispatches live as before

#### Scenario: Missing period still reports an error row
- **WHEN** no cached observation exists for the period and dispatch yields nothing
- **THEN** the per-date error row is returned as before (no synthetic value)

### Requirement: read_series tool for cached ranges

A `read_series` tool SHALL expose the internal range read: given concept, entity, and a start/end window, it returns the stored observations for that window best-ranked first, ordered by date. By default it SHALL NOT dispatch upstream — it reports held data; gaps are gaps, not surprise fetches.

The tool SHALL accept an optional `fill_gaps` flag (default `false`). When `false`, behavior is exactly the cache-only read above. When `true`, the tool SHALL apply the coverage check and gap-fill of the `price-on-demand-fill` capability: it computes the missing intervals, fetches only those, writes without overwriting existing values, and returns the merged series together with `gaps`, `fill_attempted`, and `fill_failed`. The response SHALL also carry a content fingerprint of the returned series.

The "no upstream dispatch" guarantee therefore applies to the **default** call, not to a call that explicitly asks for gap filling: a reader that does not opt in still cannot be surprised by a fetch, and a caller that does opt in has stated that the fetch is what it wants.

#### Scenario: Series over a stored window
- **WHEN** a client calls `read_series` for a concept/entity pair whose cached rows span the window
- **THEN** every stored point in the window is returned with date, value, unit, and source, and no network fetch is made

#### Scenario: Default call never fetches even when the window is incomplete
- **WHEN** `read_series` is called without `fill_gaps` for a window that is only partially covered
- **THEN** the held points are returned, no upstream call is made, and the response reports the gaps rather than silently presenting the partial series as complete

#### Scenario: fill_gaps fetches only the missing intervals
- **WHEN** `read_series` is called with `fill_gaps=true` for a window missing its first and last months
- **THEN** upstream is called only for those two intervals, already-covered intervals are not re-fetched, and the returned series covers the full window

#### Scenario: fill_gaps does not rewrite frozen history
- **WHEN** `fill_gaps=true` runs over a window whose middle is already stored and the upstream now reports a different value for an existing date
- **THEN** the stored value is unchanged and only previously-missing dates are added

#### Scenario: fill_gaps failure degrades with a labelled gap
- **WHEN** `fill_gaps=true` is requested and every upstream source fails
- **THEN** the held points are returned with `fill_attempted=true`, `fill_failed=true`, and the missing intervals listed in `gaps`; no exception is raised

#### Scenario: Window with no coverage
- **WHEN** the window contains no cached rows for the pair and `fill_gaps` is false
- **THEN** an empty series is returned with an explicit note (not an error), because this is a coverage fact, not a failure

#### Scenario: Inverted window is rejected
- **WHEN** start is after end
- **THEN** the tool returns a readable validation error

#### Scenario: Oversized window is bounded and reported
- **WHEN** the window holds more points than the server-side row cap
- **THEN** the response carries the most recent points up to the cap plus an explicit truncation note telling the caller to narrow the window (never a silent truncation, never a transport-breaking body)

### Requirement: Diagnostic error rows name the cause
When a read cannot return a value, the per-date error row SHALL distinguish the cause: no eligible source (the concept has no confirmed binding), no resolvable identifier for the entity, no dispatchable binding, or sources were attempted and all failed. A caller SHALL be able to tell a coverage gap apart from a transient failure without reading server logs.

#### Scenario: Concept without a binding
- **WHEN** a read targets a concept that has no confirmed binding and no cached row
- **THEN** the error row names the missing binding as the cause (not "no source succeeded")

#### Scenario: Entity unmapped for every candidate source
- **WHEN** candidate sources exist but none can resolve an identifier for the entity
- **THEN** the error row names the identifier gap

#### Scenario: Sources attempted and failed
- **WHEN** at least one source was attempted and every attempt failed
- **THEN** the error row reports the failed attempts (the previous generic message remains correct only for this case)

### Requirement: read_series respects entity applicability
`read_series` SHALL apply the same entity-type applicability check as `read`, rejecting a concept/entity-type mismatch rather than returning empty data for an inapplicable pair.

#### Scenario: Mismatched entity type rejected
- **WHEN** `read_series` is called with a country concept and a stock entity
- **THEN** the call fails with an applicability error, mirroring `read`
