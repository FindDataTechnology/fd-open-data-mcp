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

## [0.1.0] - 2024-07-27

### Initial Release

- Core data source registry with 7 sources (akshare, yfinance, edgar, wbgapi, cn-report, cn-gov, world)
- Basic MCP server implementation
- Database schema for sources, functions, columns
- Initial test suite
