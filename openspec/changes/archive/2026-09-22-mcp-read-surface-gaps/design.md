# Design — mcp-read-surface-gaps

## Context

All three gaps were hit in production while shipping the official site's data pipeline (2026-09-21). Established facts:

- Catalog: 2,105 concepts (452 active), `country` alone holds 1,798; `list_concepts_with_family` has a `limit` parameter (default 500) that the MCP tool never exposes — the tool signature is `(entity_type, concept_family)` only.
- Cache: 6,310,482 observations over 266 concepts (dominated by `aum` and `financials.*`; several country concepts incl. `gdp` have **zero** rows). `read_cache` matches on **exact date equality**; `dispatch_one` misses → live dispatch.
- Pod egress works (`api.worldbank.org` returns 200 from inside the deployed pod), so "no source succeeded" on uncovered concepts is a dispatch-path issue (identifier resolution / param mapping), not network.
- Internal `read_range` exists (cache read + one ranked **upstream range fetch** on cold/stale ranges) but is not exposed as a tool.
- `data_stats` default call = unfiltered `coverage_by_concept` sweep ≈ 45 s over the tailnet, and the large streamed response resets the HTTP/2 stream at the public edge (curl exit 92, 0 bytes after ~16 s).

## Goals / Non-Goals

**Goals**: clients can enumerate the whole catalog; cached observations are servable through period-aware point reads and a cache-only series read; `data_stats` default returns in seconds.

**Non-Goals**: backfilling coverage (crawl planning); fixing per-source dispatch failures (open question Q1); `concept_families` population (`concept_code` is NULL for all 2,105 rows — separate work); schema/alembic changes (none).

## Decisions

**D1 — Offset pagination, not cursors.**
`limit` + `offset` with deterministic order `(entity_type, code, id)`. At 2.7k rows offset paging has no correctness or performance problem, and it maps directly onto the existing SQLAlchemy query. Cap `limit` at 1000 (the site needs 1,798 for one type: two pages). Default stays 500 so existing callers see no change.
*Alternative rejected*: cursor/keyset pagination — warranted at much larger sizes, more client complexity.

**D2 — Period-window cache lookup, anchored on the concept's frequency.**
`read_cache` gains a period form: for a requested date `d` on a concept of frequency `f`, look for the stored observation whose **period** equals `d`'s period — implemented as a date-range query over the period window (year for `yearly`, quarter for `quarterly`, month for `monthly`; `daily`/`weekly` keep exact matching since periods collapse to the date itself). `is_stale` still decides freshness (a fully-elapsed period is never stale — existing `_period_final` logic already encodes this, so a period-window hit on elapsed periods returns without dispatch). Source-pinned reads filter within the window.
*Alternative rejected*: normalizing stored dates to period anchors on write — touches data, risks breaking the source-aware unique key; the read-side window is non-destructive.

**D3 — `read_series` is cache-only; the dispatching `read_range` stays internal.**
The internal `read_range` performs one ranked upstream range fetch for cold/stale ranges — the right behavior for the refresh pipeline, the wrong default for a public read tool (surprise latency, surprise cost, and known-broken dispatch for uncovered concepts). The new tool is a thin wrapper over `read_cache_range` + `check_applicability` + window validation: it reports **held** data, and gaps are reported as gaps. Forced refresh remains available through the existing single-point `fetch` tool.
*Alternative rejected*: exposing `read_range` as-is with a `dispatch: bool` flag — a public tool whose default can block for tens of seconds contradicts the serving goal of this change.

**D4 — `data_stats` summary = three cheap aggregates, not a shrunken sweep.**
Default call returns: total concepts (one `COUNT` on `concepts`), covered concepts + total points (one `COUNT(DISTINCT)`/`COUNT` on `semantic_observations`), per-entity-type rollup (one `GROUP BY`), plus the existing stores census. These are index-friendly aggregates even over 6.3M rows. The per-concept listing stays exactly where it is today — behind `concept_id` / `entity_type` filters — which keeps the panel's per-concept views working unchanged.
*Alternative rejected*: paginating the per-concept sweep and keeping it as the default — still a 45-second first page; the aggregate is the product, the sweep is drill-down.

**D5 — Site adoption is a follow-up in fd-official-web, not here.**
This change ships the tool surface. Switching `fetch-indicators.mjs` to `read_series` (and to paged `list_concepts`) is a small separate change in the website repo once this deploys — keeping the repos' changes independent, as with the previous round.

## Risks / Trade-offs

- [Period-window reads return "a" value for the period, not "the" date's value] → correct for period-granularity concepts (one stored observation per period by construction); documented in the tool result via the stored row's own date, which the caller sees.
- [Offset paging can skip/duplicate if the catalog changes mid-enumeration] → acceptable at this size and read patterns; ordering is deterministic so re-paging after a change is coherent.
- [`read_series` shows empty for uncovered concepts, which may read as "no data exists"] → the empty result carries an explicit coverage note; the coverage gap is a true fact, better surfaced than papered over by a hanging fetch.
- [Raising `limit` to 1000 increases per-call payload] → bounded, and smaller than the 500-row JSON the site already pulls per type today at finer granularity.
- [`data_stats` totals count observations, not distinct points] → the summary labels each figure explicitly; distinct-point counting exists in `coverage_report` for the drill-down.

## Migration Plan

1. Ship tool-surface changes behind the existing FastMCP signatures (additive arguments / one new tool) — no breaking change to current callers.
2. Verify against a dev database (a copy or read-only connection to the canonical DB) before rollout.
3. Deploy via the established image path (Jenkins/Gitee → Harbor → k3s rollout with the migrate-schema gate; it is a no-op here — no schema change).
4. After rollout: confirm `list_concepts` paging reaches 2,105, `read` returns cached values for off-peak dates on covered concepts, `data_stats` default returns <5 s end-to-end through the public `/mcp`.

Rollback: image tag revert; no data or schema to undo.

## Open Questions

- ~~Q1: Why does live dispatch fail for uncovered concepts despite working egress?~~ **Answered during implementation (task 2.3): it is not a dispatch bug — there is nothing to dispatch.** Tracing the canonical DB: `rank_sources_for_concept` returns `[]` and `dispatch_candidates` returns 0 for the probed concept, because it has **zero `concept_bindings` rows** (contrast: `price.low` has 135). With no candidates the dispatch loop body never runs, `dispatch_one` returned `None`, and `read()` filled in the generic `"no source succeeded"` — an error that reads as a transient failure for what is a coverage fact. Fixed here: `dispatch_one` now returns a cause-named error row (`no eligible source` / `no source could resolve an identifier` / `no dispatchable binding`, with the attempt-failure case unchanged). Binding the concept remains a separate, crawl-side matter.
- Q2: Should `read_series` also serve the panel UI directly (replacing its internal route), or stay MCP-only for now? MCP-only is the scope here; panel adoption can follow.
