## MODIFIED Requirements

### Requirement: Fast summary by default
Calling `data_stats` with no arguments SHALL return an aggregate summary computed without materializing per-concept rows: the catalog size (`total_concepts`), covered concepts (`covered_concepts`), the distinct-concept count holding rows (`concepts_with_observations`), the stored-row total (`total_stored_rows`), gap and stale figures, a per-entity-type rollup, and the store census. The default call SHALL complete in seconds at the current production volume (~6.3M stored rows, ~2.1k concepts).

#### Scenario: Default call returns quickly
- **WHEN** a client calls `data_stats` with no arguments against the production database
- **THEN** a summary payload returns within seconds and includes non-zero totals

#### Scenario: Summary totals are consistent with coverage_report
- **WHEN** both `data_stats` (summary) and `coverage_report` are called against an unchanged database
- **THEN** their shared figures (total concepts, covered concepts) agree

## ADDED Requirements

### Requirement: Summary figures come from the coverage aggregation
`total_concepts` and `covered_concepts` SHALL be read from the same coverage aggregation that `coverage_report` reports, not counted by a second, independent path, so the two tools cannot drift apart.

#### Scenario: Figures track the aggregation across a state change
- **WHEN** a concept gains its first stored observation and becomes covered
- **THEN** the next summary call reports the higher `covered_concepts`, equal to what `coverage_report` reports for the same database

### Requirement: Stored-row count is named for its unit
`total_stored_rows` SHALL count stored rows — multi-source rows for one observation point count separately — and its name SHALL carry that unit, because "observations" elsewhere in the tool surface means distinct observation points.

#### Scenario: Multi-source point counts once per stored row
- **WHEN** two sources hold a row for the same observation point of a concept
- **THEN** `total_stored_rows` counts both rows
- **AND** the per-concept listing counts that point once, with its distinct-source count of two
