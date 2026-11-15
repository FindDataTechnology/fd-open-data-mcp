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

### Requirement: Non-crawl producers are declared, not inferred

The repository SHALL carry a curated declaration file
(`fd-infra-deploy/tools/data/concept-producers.json`) mapping concept codes to the
producer that refreshes them when that producer is **not** the crawl planner.
Each entry SHALL state `kind` (one of `bulk_etl`, `manual_import`), `producer`
(a repository path or a policy name), `schedule` (a cron expression, or `manual`),
`last_verified` (an ISO date) and a one-line `evidence` string explaining how the
classification was established.

The routability gate SHALL treat a concept as `etl_fed` — not as a hit — only when
such a declaration exists and is not stale. Concepts with no binding and no
declaration SHALL remain `dormant` (with data) or `never_served` (without data).
Crawl-routed concepts (`kind: crawl`) SHALL NOT be declared here: their producer
is `concept_bindings`, which is already authoritative.

#### Scenario: A declared bulk-ETL concept is not reported as unroutable
- **WHEN** a concept has data, no `concept_bindings` row, and a fresh declaration
  with `kind: bulk_etl` pointing at `fd-industry-data/scripts/extract_batch2.py`
- **THEN** the gate classifies it `etl_fed`, prints the producer path and schedule,
  and does not count it as a hit

#### Scenario: An undeclared concept with data still fails the gate
- **WHEN** a concept has data and no `concept_bindings` row and no declaration
- **THEN** the gate classifies it `dormant` and, under `--baseline`, counts it as
  a hit when it is not in the baseline

#### Scenario: A new concept is never silently exempted
- **WHEN** a concept is added with no binding and no declaration
- **THEN** the gate classifies it `never_served` and prints it, so the absence of
  a declaration is visible rather than defaulting to "fine"

### Requirement: Declarations expire

每一份 `bulk_etl` / `manual_import` 声明 SHALL carry a `last_verified` date. When
that date is older than `STALE_DAYS` (default 120), the gate SHALL demote the
concept back to `dormant` and print that the declaration is stale, so that the
declaration file cannot become a permanent exemption table.

#### Scenario: A stale declaration is treated as absent
- **WHEN** a declaration's `last_verified` is 200 days old
- **THEN** the gate reports the concept as `dormant`, names the declaration as
  stale, and asks for re-verification

#### Scenario: Fresh declarations keep their exemption
- **WHEN** a declaration's `last_verified` is within `STALE_DAYS`
- **THEN** the concept stays `etl_fed` and the gate prints the producer and its
  schedule on the same line

### Requirement: The classification split is printed with its bound

The gate SHALL report the three-way split of unresolved concepts — those whose code
appears in a write-side script, those referenced only by gates/tests/fixtures, and
those with no literal occurrence in the scanned trees — **together with the
measurement bound** (`upper bound` / `lower bound`), because the criterion is a
literal-occurrence scan and cannot prove presence or absence of a producer.

#### Scenario: The bound is never omitted from the numbers
- **WHEN** the gate prints the unresolved split
- **THEN** the same output states that the first bucket is an upper bound and the
  last bucket is a lower bound, so the counts are not read as proof

### Requirement: The producer scan reads a committed ref, not a working tree

`gen_concept_producers.py` SHALL enumerate and read the write-side and reference
trees from a **git ref** (`<repo>:<ref>:<subdir>`, resolved via `git ls-tree` /
`git show`), never from whatever happens to be checked out in a working tree.
The declaration file's `scope` section SHALL record, for each scanned tree, the
repo, the ref and the **resolved commit sha**, plus the `.py` file count, so that
a reader can reproduce the split without guessing which version was measured.

A ref that cannot be resolved SHALL be a hard failure (`EXIT=2`, "判不出") and
SHALL NOT degrade to "zero write-side hits". Zero hits is the most dangerous
success: it reads as "nothing produces these concepts" while the real cause is
that the scan never saw the code.

#### Scenario: A stale checkout does not change the split

- **WHEN** the local working tree of `fd-industry-data` is behind `gitee/main`
  by any number of commits
- **THEN** the reported three-way split and `.py` count equal the values measured
  from the ref, so the numbers do not depend on which branch someone has checked out

#### Scenario: The split names the ref and the commit it resolved to

- **WHEN** the declaration file is regenerated
- **THEN** its `scope` names each tree as `<repo>:<ref>:<subdir>`, records the
  resolved commit sha and the per-tree `.py` count, and the printed split line
  reports the total `.py` count actually read

#### Scenario: An unresolvable ref fails instead of reporting zero

- **WHEN** a declared tree's ref does not exist (typo, un-fetched remote, wrong repo)
- **THEN** the generator exits `2` with the offending tree named, and does not
  write a declaration file whose split says `写侧命中 0`

#### Scenario: A provenance-only change does not turn the fixed-point check red

- **WHEN** upstream commits advance but the scanned content is unchanged, so the
  recorded `scope.commits` shas no longer match the current refs
- **THEN** the fixed-point check (`--check`) still passes and prints the recorded
  and current commits as a note, because the commit shas are provenance (they
  participate in the record) and not part of the criterion (they must not make
  the check red on every upstream push — a gate that is red every day is ignored)

#### Scenario: Content drift still fails the check

- **WHEN** a script in the scanned ref changes such that the set of concept-code
  literals changes
- **THEN** `--check` reports the declaration file as needing regeneration and
  names the commit drift, so content staleness stays a red condition even though
  provenance staleness is not
