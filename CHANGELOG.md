# Changelog

All notable changes to this project will be documented in this file.

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
