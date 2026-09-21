## Purpose
The `data_stats` reporting shape: a fast aggregate summary by default, with the per-concept coverage detail available only on explicit request — so the tool stays usable as the observation volume grows.

## ADDED Requirements

### Requirement: Fast summary by default
Calling `data_stats` with no arguments SHALL return an aggregate summary computed without materializing per-concept rows: the catalog size (`total_concepts`), the covered-concept count (`covered_concepts`), the raw distinct-concept count holding rows (`concepts_with_observations`), the stored-row total (`total_stored_rows`), the gap and stale figures, a per-entity-type rollup, and the store census. `total_stored_rows` counts stored rows — multi-source rows for one point count separately — and its name SHALL carry that unit, because "observations" elsewhere in the tool surface means distinct observation points. `total_concepts` and `covered_concepts` SHALL come from the same coverage aggregation that `coverage_report` reports, so the two agree by construction. The default call SHALL complete in seconds at the current production volume (~6.3M stored rows, ~2.1k concepts).

#### Scenario: Default call returns quickly
- **WHEN** a client calls `data_stats` with no arguments against the production database
- **THEN** a summary payload returns within seconds and includes non-zero totals

#### Scenario: Summary totals are consistent with coverage_report
- **WHEN** both `data_stats` (summary) and `coverage_report` are called against an unchanged database
- **THEN** their shared figures (total concepts, covered concepts) agree

### Requirement: Per-concept detail on explicit request
The per-concept coverage listing SHALL be returned only when narrowed — via the existing `concept_id` filter, an `entity_type` filter, or an explicit detail flag with pagination — never as part of the unfiltered default response.

#### Scenario: Single-concept detail
- **WHEN** `data_stats` is called with a `concept_id`
- **THEN** the per-concept coverage row (point count, latest date, distinct sources, last fetch) is returned

#### Scenario: Unfiltered response stays small
- **WHEN** `data_stats` is called with no arguments
- **THEN** the response does not contain a per-concept array sized by the catalog

### Requirement: Response streams survive the public edge
The unfiltered summary response SHALL be small enough to transfer through the public HTTPS edge without the stream being reset (the previous unfiltered response broke HTTP/2 transfer at ~16 s / large body).

#### Scenario: Default call succeeds end to end through /mcp
- **WHEN** a client calls `data_stats` (no arguments) through the public `/mcp` endpoint
- **THEN** the complete response body arrives without a transport error
