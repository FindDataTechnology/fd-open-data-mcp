## ADDED Requirements

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
