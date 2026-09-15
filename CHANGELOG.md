# Changelog

All notable changes to this project will be documented in this file.

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
