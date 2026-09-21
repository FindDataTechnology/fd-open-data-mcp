# Tasks — mcp-read-surface-gaps

## 1. Catalog pagination

- [ ] 1.1 Expose `limit`/`offset` on the `list_concepts` tool (cap `limit` at 1000, default 500) and thread them through `list_concepts_with_family` with deterministic ordering `(entity_type, code, id)`; same for `list_concept_families`. Verify: against the canonical DB, paging with `limit=1000` concatenates to exactly 2,105 rows with no overlap/gap; a `country`-filtered page sequence reaches all 1,798.
- [ ] 1.2 Add unit tests for ordering stability and page-boundary behavior (existing tests in the repo's suite keep passing — the no-argument default is unchanged). Verify: test run green, including a case with `limit` > result size.

## 2. Period-aware cache reads

- [ ] 2.1 Add a period-window lookup to `read_cache` (year/quarter/month windows from the concept's frequency; exact match for daily/weekly) and use it in `dispatch_one`'s cache check, preserving source pinning and `is_stale` semantics. Verify: against a covered concept (e.g. `financials.revenue` for a stock entity), reading an off-peak date inside an elapsed period returns the cached row with `from_cache: true` and no upstream call (assert via instrumentation/log absence).
- [ ] 2.2 Unit tests: period hit at window edge dates, current-period staleness still dispatching, exact-match daily behavior unchanged, source-pinned window lookup.
- [ ] 2.3 Diagnose the uncovered-concept dispatch failure (design Q1): trace one `read(gdp, country:AU, date)` through `dispatch_candidates` → `resolve_identifier` → `_build_params` and record the failing step in this change's design notes. Fix only if it is a contained adapter bug; otherwise file the finding for a follow-up change.

## 3. read_series tool

- [ ] 3.1 Add the `read_series` tool: cache-only range read (`read_cache_range`) + `check_applicability` + window validation (start ≤ end, bounded max span), returning date/value/unit/source rows plus an explicit coverage note on empty windows. No upstream dispatch anywhere in its path. Verify: a covered pair returns every stored point in the window; an uncovered pair returns an empty series with the note; an inverted window returns a validation error; a type-mismatched pair raises the applicability error.
- [ ] 3.2 Unit tests for the tool wrapper (validation, applicability, cache-only behavior with the dispatch layer mocked to fail loudly if called).

## 4. data_stats summary

- [ ] 4.1 Change `data_stats` default to the summary shape (design D4: total concepts, covered concepts, total points, per-entity-type rollup, stores census); keep the per-concept listing behind the existing `concept_id`/`entity_type` filters. Verify: unfiltered call against the canonical DB completes < 5 s and agrees with `coverage_report` on shared figures; a `concept_id` call still returns the per-concept row.
- [ ] 4.2 End-to-end check through the public edge: `data_stats` default over `https://www.finddatatech.cloud/mcp` returns the full body with no transport error (the previous unfiltered call reset the stream).

## 5. Verify + ship

- [ ] 5.1 Full verification against a dev database (or read-only canonical connection): all new tool scenarios from the three spec files exercised once each. Verify: each scenario's WHEN/THEN observed directly.
- [ ] 5.2 Build and roll the image via the established path (Jenkins/Gitee → Harbor → k3s rollout; migrate-schema gate is a no-op). Verify: pod Ready; post-rollout spot checks — `list_concepts` paging total = 2,105, off-peak cached read, `data_stats` default < 5 s through the public endpoint.
- [ ] 5.3 Record the Q1 diagnosis outcome in this change's design.md (fixed here, or filed as follow-up). Verify: design.md updated with the finding and its disposition.
