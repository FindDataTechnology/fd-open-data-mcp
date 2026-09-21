## Purpose
The `data_stats` reporting shape: a fast aggregate summary by default, with the per-concept coverage detail available only on explicit request — so the tool stays usable as the observation volume grows.

## ADDED Requirements

### Requirement: Fast summary by default
Calling `data_stats` with no arguments SHALL return an aggregate summary (total concepts, covered concepts, total observation points, per-entity-type rollup, store census) computed without materializing per-concept rows. The default call SHALL complete in seconds at the current production volume (~6.3M observations, ~2.7k concepts).

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
