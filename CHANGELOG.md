# Changelog

All notable changes to this project will be documented in this file.

## [0.5.35] - 2026-10-01

### Fixed (image-slimming)

#### FastembedEncoder implements get_embedding_dimension

0.5.34's encoder missed the SentenceTransformer dimension probe that
semantic_search calls on every query — every semantic_search call on the
fastembed backend failed with AttributeError. The probe is now computed
once and cached, and a surface-contract unit test pins every model.* method
the search paths call so a backend swap cannot silently miss one.

## [0.5.34] - 2026-10-01

### Changed (image-slimming)

#### Browser stack split out of the `[data]` extra

`scrapling` and `playwright` move from `[data]` to a new `[browser]` extra.
Nothing in the crawl fleet imports them (verified 2026-10-01: zero
`scrapling` references in fd-open-data-mcp and scraw-fd-open-data-mcp;
playwright only in the uncalled `scraping/browser.py` helper, which also
speaks the remote-browser `BROWSER_CDP_URL` mode), and the industry line
installs its own playwright+chromium stack in its image. Since the fleet
image resolves `fd-open-data-mcp[data]` from PyPI at build time, keeping
the browser packages in `[data]` would have silently re-fattened every
future fleet rebuild. `pip install fd-open-data-mcp[data]` no longer pulls
the browser stack; `pip install fd-open-data-mcp[data,browser]` restores
it where browser rendering is actually needed.

#### Embedding backend chain: fastembed (onnxruntime) first, sentence-transformers fallback

`[search]` now installs `fastembed`, serving the same MiniLM weights
(`sentence-transformers/all-MiniLM-L6-v2`) as official ONNX exports on a
~15MB onnxruntime instead of torch's ~195MB compressed runtime;
`sentence-transformers` moves to `[search-legacy]` for exotic
`FD_MCP_EMBEDDING_MODEL` values outside fastembed's catalog. The shared
facade (`fd_open_data_mcp.embeddings.model`) resolves fastembed first and
falls back to sentence-transformers; `generator.py` and
`semantic/entity_search.py` no longer instantiate `SentenceTransformer`
directly, so no backend bypass remains. The vector contract is unchanged —
same model identity, same 384 dims, `encode(str)` 1-D / `encode(list)` 2-D
semantics — and failures still surface naming the model.

#### Slim image runtime env cleanup

The plain (non-torch) Dockerfile no longer bakes the tuna pip index into
the runtime image's ENV; CN mirrors are build-time concerns only.

## [0.5.33] - 2026-09-29

### Fixed (panel-indicator-observatory)

#### Domain graph view crashed on PostgreSQL

The registry-domain neighborhood query aggregated any-verified with
`max(verified)` — PostgreSQL has no `max()` for boolean (SQLite, the test
backend, accepted it, so unit tests stayed green). Replaced with the
portable `count(*) FILTER (WHERE verified) > 0`; verified against the live
production database.

## [0.5.32] - 2026-09-29

### Added (panel-indicator-observatory wave 2)

#### Scope management pages

`/panel/indicators/scopes` completes the observatory board now that
`registry-transparent-read-and-scope` has landed: scope list with rules and
7-day hit totals, create / edit / delete forms over the four allow-list
dimensions (source_dbs / domains / semantic_codes / native_codes,
comma-separated, blank = unconstrained), and a per-scope detail page with
per-day hit statistics. The routes call the same `scoping` service functions
the MCP scope tools call (no HTTP hop), so a scope created in the panel is
immediately usable via the tools; empty-scope and reserved-name refusals
surface as explicit errors.

## [0.5.31] - 2026-09-29

### Added (registry-transparent-read-and-scope)

#### Transparent federated reads (concept-fetch delta)

`read` / `read_series` now accept registry-only indicators — pass the
semantic_code (or native_code) as `concept_id` — and route them, by the
entry's source_db, to the business-mcp domain read tool (`yearbook_read` /
`wb_read` / `gta_read` / `china_city_panel → city_read`), projecting results
into the standard read row shape; provenance travels in `source_used`.
Rollout flag `FD_MCP_FEDERATED_READ` defaults OFF (staged per-domain
rollout; endpoint via `FD_MCP_BUSINESS_URL`). Failure semantics are loud and
explicit: `federated_read_disabled`, `no_read_channel` (names the source),
`federation_unavailable` (with a short retry cooldown). Local concept reads
never touch the federation path.

#### read_via hints (open-data-catalog delta)

Catalog (`list_concepts`) and search (`ai_search` / `semantic_search` /
`semantic_search_unified`) outputs attach to every registry entry a
`read_via {tool, args}` hint naming the domain tool + native-code argument;
sources without a read channel carry `read_via: null` rather than a wrong
hint. Local concepts are never labeled registry-only.

#### Retrieval scopes (indicator-scope)

Named allow-list scopes `{source_dbs, domains, semantic_codes, native_codes}`
stored in `fd_open_data` (`scopes` / `scope_bindings` / `scope_stats`,
migration `0004_scope_tables`). Management tools `scope_create` /
`scope_list` / `scope_update` / `scope_delete` (empty-scope validation
against live tables with the match count returned; unknown code values
warn without blocking; `unscoped` reserved). All search/read tools take an
optional `scope` parameter — out-of-scope reads return an explicit
`out_of_scope` response, scoped responses disclose `{name, summary}`, scoped
emptiness is explainable. Caller default bindings via `scope_bind_caller` /
`scope_unbind_caller` (opaque caller key from `X-FD-Caller` or
`FD_MCP_CALLER_KEY`; explicit parameter and the reserved `unscoped` always
win). Per-(scope, day) hit counters served by `scope_stats`. The same
scope semantics run inside business-mcp's domain search tools
(single-source: admit → disclose+count, exclude → explainable empty).


## [0.5.30] - 2026-09-29

### Added (panel-indicator-observatory)

#### Indicator observatory board

New panel board「指标 Indicators」under `/panel/indicators/*` — the observability
surface over the 57k-entry unified indicator registry and the concept layer.
Reads the same tables the MCP tools serve through the same session (a view,
never a fork); sits behind the existing auth/role gate like every board.
Five views:

- **Registry table** (`/panel/indicators`): domain / source_db / verified /
  keyword filters, server-side paging (50/page), unverified entries visible
  with an unmistakable badge (ops caliber — distinct from the public
  catalog's verified-only gate).
- **Concept family tree** (`…/families`): families → member concepts, each
  with its binding count; per-family drill-down.
- **Relation browsing** (`…/relations`): cross-source binding list (native
  codes) + mappings searchable by external vocabulary, SKOS relation type
  and keyword; per-indicator pages list the registry anchors sharing a
  semantic code — the indicator's cross-source equivalence set.
- **Coverage** (`…/coverage`): registered/verified per source database and
  domain as grouped SVG bars + tables — the identical GROUP BY
  `registry_coverage` runs on the same database, so the two surfaces agree
  at the same moment.
- **Interactive relation graph** (`…/graph`): vendored zero-build
  vis-network 9.1.9 (UMD, ~675KB) served from the panel's same-origin
  static mount — no CDN, no build step; bounded neighborhood views by
  family / indicator / domain with server-side depth (≤2) and node (≤200)
  caps; node detail panel; theme tokens and bilingual labels follow the
  panel's active settings; degrades to a server-rendered relation listing
  inside `<noscript>` when client scripting is unavailable.

New module `panel/observatory.py` (fail-soft registry reads like
`semantic.registry_catalog`), `charts.grouped_bar_geometry`, 15 tests in
`tests/test_panel_indicators.py` covering every spec scenario incl. the
same-source fork guard and the self-hosted/no-CDN page-source contract.
Scope management pages are deliberately out of this wave — they wait for
change `registry-transparent-read-and-scope` to land.

## [0.5.29] - 2026-09-29

### Fixed (mcp-search-engine-overhaul P0)

#### semantic_search resolves its model by name

`semantic_search.py` loaded the embedding model from a hardcoded dev-machine
cache path, so every `semantic_search` / `semantic_search_unified` call in the
production container failed with a filesystem error. Model resolution now
lives in `fd_open_data_mcp/embeddings/model.py`, resolves by name (env
`FD_MCP_EMBEDDING_MODEL`), and failures surface as a tool error naming the
model.

#### Each tool registered exactly once

`semantic_search.py`, `ai_search.py` and `entity_graph_tools.py` carried
module-level `@mcp.tool()` registrations that re-registered (and overrode)
the server-level definitions the first time a tool call lazily imported them.
Those registrations are gone — `server.py` is the only registration point.

#### Retrieval caches actually survive across calls

The graph manager, entity semantic search and their caches (graph TTL,
embedding query cache) were rebuilt per call, making the 300s TTLs useless.
Both engines are now process-wide lazy singletons (`fd_open_data_mcp/engines.py`),
shared with fd-find-data-business-mcp's delegated `graph_search`.
`GRAPH_CACHE_TTL` / `EMBEDDING_CACHE_SIZE` are wired; `CACHE_ENABLED` /
`SEARCH_RESULT_CACHE_TTL` gate a new search-result TTL cache whose hits are
marked `"cached": true`. Write tools (`update_entity` / `add_entity` /
`add_relationship` / `update_concept` / `re_embed_concept`) invalidate caches
immediately instead of waiting out the TTL.

#### ai_search value layer runs on SQLite dev databases

Layer 3 used PostgreSQL-only `ANY()` / `DISTINCT ON`; now portable expanding
`IN` + `ROW_NUMBER()` window, verified end-to-end on SQLite.

#### Housekeeping

- `semantic_search` / `semantic_search_entities` / `semantic_search_unified`
  now return `{cached, count, results}` envelopes so cache hits stay
  distinguishable.
- Hardcoded DSNs (with a stale password placeholder) removed from
  `scripts/migrate_add_vector_search.py`, `scripts/generate_entity_embeddings.py`
  and `scripts/migrate_entity_sync_schema.py`; they now require
  `FD_OPEN_DATA_MCP_DATABASE_URL`.
- Dead `.env.example` keys removed (`EMBEDDING_MODEL` → `FD_MCP_EMBEDDING_MODEL`,
  `CACHE_TTL_HOURS` → `SEARCH_RESULT_CACHE_TTL`).

### Added (mcp-search-engine-overhaul 3.x/4.x) — shipped 2026-09-29 as sha-6ff2017..a0f2dec

- **pgvector + HNSW vector engine** (ADR 0001): alembic 0003 adds
  `embedding_vec vector(384)` columns + batched backfill + HNSW cosine
  indexes + `registry_indicator_embeddings`; reads execute inside
  PostgreSQL (`FD_MCP_VECTOR_BACKEND=pgvector`, live since sha-a0f2dec).
  Dual-read verification passed (concept 1.00/1.00, entity 0.983/1.00
  Jaccard/Kendall). Two pgvector defaults proved unsafe and are now set
  per-session: `hnsw.ef_search=200` (beam missed exact neighbors in
  duplicate clusters) and `hnsw.iterative_scan=relaxed_order` (filtered
  queries returned EMPTY when the beam neighborhood failed the filter).
- **Registry corpus in semantic search**: all 57,552 registry entries
  embedded (57,552 rows / 1,052 s, local inference; 1 all-empty skipped);
  verified-gated at query time (live JOIN, 3,420 visible, 54,132
  embedded-but-invisible); registry-only indicators surface with
  `result_type: "registry"` (verified live: "grain output" → city panel
  Grain Output ranks first).
- **Transitional matrix backend** (`FD_MCP_VECTOR_BACKEND=matrix`):
  in-process numpy cache with `VECTOR_MATRIX_TTL`, equivalence-tested
  against the json backend on SQLite.
- **sqlalchemy pinned <2.1**: 2.1.x (fresh on the build mirror) removes the
  psycopg2 fallback for `postgresql://` and crashed the migrate-schema
  initContainer; pin restores the tested dialect.
- Reference k8s manifest aligned with the live fd-official-web deployment
  (was a stale zihan-era copy: 512Mi / harbor.local / NodePort 30801).

## [0.5.16] - 2026-09-18

### Fixed

#### Requests that can never succeed no longer get retried

The retry, circuit-breaker and ranking machinery could express "this route is
unhealthy" but not "this request is impossible", so a missing endpoint was
classified `transient` and retried against proxies it had nothing to do with.
Between 2026-08-30 and 2026-09-16 the fleet logged **5,277,961** `has no
callable` attempts, and on 09-16 it issued roughly 266,000 failed fetches in 24
hours with **zero** successes.

- **`permanent` failure class** — a new `ban_rules` classification, seeded for
  the `has no callable` family. A permanent failure is not retried and does NOT
  touch a proxy circuit or the outcomes stream: the exit is not at fault.
- **`fetch/capability.py`** — static, no-I/O resolution of `(source, command)`.
  Whether a callable exists is decidable without spending a network request.
- **Entity-domain agreement** — rules matched on column *name* alone, so every
  akshare endpoint with a `收盘`/`close` column bound to `price.close`/`stock`:
  options, futures, bonds, funds and indices among them (concept 234 reached
  **117** dispatch-eligible bindings). A proposal is now refused when the
  function's declared domain and the rule's concept domain disagree. Domain
  comes from a command prefix, an instrument word, or the source; instrument
  words are checked before prefixes, or `stock_board_industry_index_ths` is
  claimed by `stock_`.

### Changed

- **Binding eligibility needs evidence, not just a score** — the dispatch gate
  was satisfiable by `confidence >= 0.6` alone and the rule table emits
  0.85–0.9, so every machine proposal was live and the review step gated
  nothing. An `llm`-provenance binding now requires confirmation; confidence is
  a floor, never a substitute. Ships **report-only**
  (`FD_BINDING_ELIGIBILITY_GATE`) so a mis-written gate cannot narrow a live
  crawl before it is reviewed.
- **`confirm_by_rule` / `verify_functions_by_rule`** — rule-based binding
  confirmation and function verification. Both require positive evidence
  (domain agreement AND resolvability) and refuse rather than assume. A
  confirmation records the distinct provenance `rule-confirmed`.
- **Bounded failover chain** — `FD_CRAWL_CHAIN_MAX` (default 8). Candidates
  past the bound are reported as not-attempted rather than dropped silently.
- **Permanent-path suppression** — a `(concept, function)` whose recent
  outcomes are all permanent is excluded outright rather than reordered
  ("last" is still "attempted"). Derived from `fetch_log`, computed once per
  plan, clearable by data. Suppression is **cluster-independent** because a
  missing callable is missing from every egress — unlike demotion, which is
  route health and stays per-cluster.

Note there are **two** gates: a binding is dispatchable only when the binding
is eligible AND its function is verified. Of the 536 bindings confirmed by the
new rule, 508 sat on `verified = false` functions and remained unreachable.

## [Unreleased]

### Added

#### Multi-source observations (`add-multi-source-observations`)

- **Sources coexist per observation point** — the `semantic_observations`
  unique key gains `source_used` (`concept, entity, date, granularity,
  source`), so WorldBank's China-2024 GDP and NBS's are stored as separate
  attributed rows instead of first-writer-wins. Values are never merged.
- **Query-time source selection** — a plain `read` returns the highest-ranked
  source's row (`source_rankings`, effective on the next read with no row
  rewrites); `read(source=...)` pins cache reads and dispatch to one source;
  `read(all_sources=true)` returns every held row per point, best-ranked
  first, without dispatching. Same parameters on the MCP `read` tool and the
  `fd-open-data-mcp read` CLI (`--source`, `--all-sources`).
- **Source-scoped watermarks** — `since_last` crawl plans compute their
  watermark over the policy's `source_filter` when set, so a second source's
  backfill plan starts from what that source itself holds.
- **Coverage counts points, not rows** — `/panel/data` and `data_stats`
  report distinct observation points plus a per-concept `sources` count, so
  coexistence never inflates coverage.
- **Online migration** — `fd-open-data-mcp migrate` swaps `uq_sem_obs` to the
  source-aware key (Postgres: concurrent index build + constraint swap under
  an advisory lock; SQLite: table rebuild). Data-preserving by construction
  (the relaxed key admits every existing row). `migrations/
  007_multi_source_observations[.rollback].sql` is the manual runbook; the
  rollback dedupes to the highest-ranked source per point and reports what
  it removed. Requires `scraw-fd-open-data-mcp` deployed against the same
  schema (its writer names the 6-column conflict target).

#### New Data Source Adapters (21 sources)

- **NBS GDP** (`nbs-gdp`): National Bureau of Statistics macroeconomic data
  - Quarterly GDP data with akshare fallback
  - Monthly CPI, PPI, PMI indicators
  - Direct API integration with stats.gov.cn

- **CISA Industry** (`cisa-industry`): China Iron and Steel Association data
  - Steel production statistics
  - Market statistics and pricing

- **AMAC Fund** (`amac-fund`): Asset Management Association of China
  - Fund registration and statistics
  - Manager registration data

- **SHFE Futures** (`shfe-metal-futures`): Shanghai Futures Exchange
  - Metal futures pricing (Cu, Al, Zn, Pb, Ni, Sn)
  - Volume and open interest data

- **SAC Securities** (`sac-securities`): Securities Association of China
  - Securities trading statistics
  - Market turnover data

- **Agriculture** (`agriculture`): Dalian Commodity Exchange
  - Agricultural futures pricing (soybean, corn, PP, JB)
  - Volume and open interest tracking

- **CME Agricultural** (`cme-agricultural-futures`): CME Group
  - Grain futures pricing (corn, wheat, soybean, oat)
  - Soy meal data with USD→RMB conversion

- **Chemicals** (`chemicals`): SCI99 chemical industry data
  - Basic chemical product prices (PVC, methanol, ethylene, propylene)
  - Chemical industry PMI and production indices

- **Electronics** (`electronics`): CEIA electronics association
  - Semiconductor industry statistics
  - Electronic circuit, display panel, consumer electronics output

- **Nonferrous Metals** (`nonferrous`): CNIA non-ferrous metals data
  - Aluminum, copper, lithium pricing and inventory
  - Import/export statistics

- **Flowers KIFC** (`flowers-kifc`): Kunming flower auction center
  - Daily flower auction prices (rose, lily, orchid, tulip)
  - Trading volume and buyer statistics

- **Financial Platforms** (`fin_platforms`): Wind financial terminal
  - Market benchmark indices (SH50, SZ300, HSI, NASDAQ, S&P500)
  - Sector performance tracking
  - Fund ranking statistics

#### Infrastructure Improvements

- Added `errors.py` module with custom exception types
- Enhanced `runner.py` with lazy loading for all adapters
- Added comprehensive test suite in `tests/test_runners.py`
- Updated manifests with `fetch.runner` and `ranking_seed` fields
- Integrated caching layer for improved performance

#### Documentation

- Updated README with usage examples and rate limit policies
- Created deployment guide in `docs/DEPLOYMENT.md`
- Added comprehensive task tracking in OpenSpec workflow

### Changed

- Updated `pyproject.toml` to include new dependencies (requests, beautifulsoup4, scrapling)
- Enhanced manifest schema compliance with fd-open-data-protocol
- Improved error handling across all data source adapters

### Fixed

- Resolved import errors for new adapter modules
- Fixed runner routing for all 21 new data sources
- Corrected manifest YAML syntax validation

## [0.5.15] - 2026-09-15

### Added

#### Semantic vocabulary core (two-level concept model + crosswalk)

- **Concept families** — new `concept_families` table, materialized from
  `fd-open-data-protocol`'s `vocabulary/concepts.yaml`, with `concepts.concept_code`
  linking every Variable to exactly one family. Variables without an explicit
  family get one derived from the concept code.
- **`consume-concepts` repointed** from the (absent) `fd-entities-indicators`
  sqlite to the protocol vocabulary — seeding now works on a clean checkout.
- **Concept crosswalk** — new `concept_mappings` table asserting Variable ↔
  external-vocabulary equivalences (SKOS relation, confidence, provenance,
  review state), plus `crosswalks/*.yaml` ingestion.
- **External entity anchors** — `wikidata` (QID) and `datacommons` (DCID) as
  per-source identifier sources, resolvable back to the local entity; manifest
  `entity_definitions[].metadata.external_ids` persists anchors at registration.
- **New tools** — `list_concept_families`, `record_concept_mapping`,
  `list_concept_mappings`, `import_crosswalks`; `list_concepts` now reports each
  Variable's family and filters by it; `resolve_entity` also resolves an
  external anchor to its entity; `get_entity`/`list_entities` expose anchors.
- **New CLI commands** — `import-crosswalks`, `list-concepts`,
  `list-concept-families`.

### Changed

- Requires `fd-open-data-protocol>=0.3` (for the shared crosswalk relation set).

## [0.1.0] - 2024-07-27

### Initial Release

- Core data source registry with 7 sources (akshare, yfinance, edgar, wbgapi, cn-report, cn-gov, world)
- Basic MCP server implementation
- Database schema for sources, functions, columns
- Initial test suite
