## Why

The official website's data work (change `official-web-live-data-refresh`, archived 2026-09-21 in fd-official-web) surfaced three serving-layer gaps in the deployed MCP. None is speculative — each was hit in production during that change:

1. **`list_concepts` cannot enumerate the catalog.** The tool exposes no `limit`/cursor and the implementation caps a call at 500 rows; the catalog holds 2,105 concepts (country alone: 1,798). A client cannot fetch them all — the site's per-entity-type fan-out recovers the active set by luck of distribution, and loses any type whose rows exceed the cap.
2. **Cached observations are almost unservable through `read`.** 6.31M rows sit in `semantic_observations` (266 concepts with coverage), yet `read()` only hits the cache on an **exact date match** — a caller who asks for `2020-01-01` on a yearly concept whose row is stored at any other date gets a cache miss, a live dispatch, and (for concepts with no upstream fetch path working) `"no source succeeded"`. The internal `read_range` series path exists but is **not exposed as a tool at all**, so there is no way to ask for "this series over this range" from MCP.
3. **`data_stats` is unusable at this catalog size.** Its default (no-filter) call aggregates per-concept coverage over 6.31M rows through the tailnet link (~45 s server-side), and the resulting stream breaks at the public HTTP/2 edge. The website avoids it; the panel's `/panel/data` page powers off the same function.

## What Changes

- **`list_concepts` gets pagination**: `limit` (capped, default raised) and `offset` parameters, preserving the existing per-`entity_type` filter; callers can page through the full catalog. `list_concept_families` gets the same treatment for symmetry.
- **`read` becomes period-aware on the cache path**: a date that falls inside a fully-elapsed period (e.g. any day of 2020 for a yearly concept) resolves to that period's stored observation instead of exact-matching and dispatching. Fresh-period and `source`-pinned semantics are unchanged; live dispatch still happens only on cache miss/stale.
- **New `read_series` tool**: exposes the existing internal range-read over the cache (start/end window, per-concept × per-entity), returning stored rows best-ranked first, without dispatching — the bulk counterpart to `read`'s point reads, and the source the site's sample-series enrichment switches to.
- **`data_stats` gets a fast default**: a `summary` mode (or a default summary shape) that returns totals without the per-concept sweep; the per-concept detail stays available behind explicit `concept_id`/`entity_type` filters or a paged detail mode. The default call must return in seconds, not tens of seconds.

## Capabilities

### New Capabilities
- `catalog-enumeration`: paging through the concept catalog and concept families over MCP — every list tool returns bounded pages and can enumerate the full set.
- `observation-reading`: serving cached observations over MCP — period-aware point reads, a range/series read tool, and cache-first (no surprise dispatch) semantics for bulk reads.
- `stats-reporting`: the `data_stats` shape — fast aggregate summary by default, per-concept detail on explicit request.

### Modified Capabilities
<!-- None — the three existing specs (database-adapter, entity-graph-networkx,
     entity-semantic-search, sync-lock-abstraction, wbgapi-datasource) cover
     other layers; the MCP tool surface had no spec. -->

## Impact

- **Server tools**: `fd_open_data_mcp/server.py` (`list_concepts`, `list_concept_families`, `data_stats` signatures), new `read_series` tool; `fd_open_data_mcp/semantic/concepts.py` (limit/offset plumbing), `fd_open_data_mcp/fetch/cache.py` + `dispatch.py` (period-aware cache lookup), `fd_open_data_mcp/policy_tools.py` / `visibility/coverage.py` (summary shape).
- **Consumers**: the official site's `fetch-indicators.mjs` switches from guessed-date `read` calls to `read_series` (sample-series coverage should go from 0 to "whatever the cache holds"); the panel's `/panel/data` page keeps working (summary by default).
- **No schema changes**: all fixes are tool-surface; `semantic_observations` and the alembic chain are untouched.
- **Out of scope**: backfilling coverage for uncovered concepts (gdp/country has zero rows — a crawl-planning matter, not a serving matter); diagnosing why live dispatch fails for specific sources (egress from the pod is confirmed working — worldbank returns 200 — so per-source dispatch failures need separate debugging; recorded in design as a follow-up question); `list_concepts` returning >500 rows in a single call (pagination is the contract, not unbounded responses).
