# catalog-enumeration Specification

## Purpose
Paging through the concept catalog over MCP: every list tool on the catalog (concepts, concept families) returns bounded pages and can enumerate the complete set, so no client is silently truncated by a server-side row cap.

## Requirements

### Requirement: list_concepts pagination
The `list_concepts` tool SHALL accept `limit` and `offset` arguments. `limit` SHALL be capped at a server-side maximum (at least 1000) with the default matching the current behavior for backward compatibility, and `offset` SHALL skip rows in a stable ordering (entity_type, code, id). A client SHALL be able to enumerate the full catalog — every concept row reachable by paging — regardless of catalog size or single-type row counts.

#### Scenario: Page through a large catalog
- **WHEN** a client calls `list_concepts` repeatedly with `limit` and increasing `offset`
- **THEN** the concatenated pages contain every concept exactly once, with no gap or duplicate at page boundaries

#### Scenario: Single type exceeding the row cap
- **WHEN** one entity_type holds more rows than the per-call maximum (e.g. 1,798 country concepts)
- **THEN** paging with that `entity_type` filter reaches all of its rows

#### Scenario: Backward-compatible default
- **WHEN** a client calls `list_concepts` with no pagination arguments
- **THEN** the call succeeds with the existing default page size and unchanged result shape

### Requirement: Stable page ordering
Pages SHALL be ordered deterministically (entity_type, then code, then id) so that offset-based paging is consistent across calls within a catalog version.

#### Scenario: Consecutive pages do not overlap or skip
- **WHEN** two calls are made with `offset` 0 and `offset = limit` against an unchanged catalog
- **THEN** no row appears in both pages and no row between them is missing

### Requirement: list_concept_families pagination
The `list_concept_families` tool SHALL accept the same `limit`/`offset` arguments with the same semantics, so the family listing can be enumerated when it is populated.

#### Scenario: Families page cleanly
- **WHEN** `list_concept_families` is called with `limit`/`offset`
- **THEN** it returns a bounded, deterministic page of families
