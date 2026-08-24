# wbgapi-datasource Specification

## Purpose
TBD - created by archiving change broaden-crawl-scope. Update Purpose after archive.
## Requirements
### Requirement: wbgapi adapter registered at adapters import

The `fd_open_data_mcp.adapters` package SHALL register a `get_indicator_data`
adapter at import time so the scraw `fetch_handler` builds the params
`{economy, indicator, date}` (economy <- resolved entity identifier,
indicator <- the binding column name = WDI series code, date <- the requested
year) instead of the legacy `{symbol, date}` fallback that `run_wbgapi`
rejects. The adapter registration SHALL be unconditional (the adapter module
SHALL have no top-level third-party imports; `pandas` and `wbgapi` SHALL be
imported lazily inside the extract methods) so it is safe in every environment
that imports the adapters package — including the scraw worker image before
the `data` extra is installed.

#### Scenario: fetch_handler builds wbgapi-correct params

- **WHEN** the concept-crawl executor fetches a WDI concept whose binding maps
  to the `wbgapi` provider's `get_indicator_data` function
- **THEN** `fetch_handler` builds `{economy, indicator, date}` (not
  `{symbol, date}`)
- **AND** `run_wbgapi` accepts the params and reshapes the result for value
  extraction

#### Scenario: adapters package imports without wbgapi installed

- **WHEN** the `fd_open_data_mcp.adapters` package is imported in an
  environment where the `wbgapi` package is not installed
- **THEN** no `ModuleNotFoundError` is raised
- **AND** the `get_indicator_data` adapter is registered (lazy imports defer
  the `wbgapi` dependency to call time)

