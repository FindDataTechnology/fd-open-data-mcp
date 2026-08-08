# fd-open-data-mcp

An **open-data ontology MCP**: a semantic concept layer over multi-datasource
financial/economic data. You ask for data in **concepts + entities** (e.g.
"price.close for Moutai", "GDP for China"); the system resolves the concept to
physical columns across datasources, ranks candidate sources by quality +
accessibility, fetches from the best one (with failover), caches by concept,
and refreshes on a per-concept frequency.

It consumes the finddata `fd-*` datasource registries and
`fd-entities-indicators` **read-only** and adds the unifying layers on top.

## Architecture

```
CONSUMED (read-only)                  ADDED by fd-open-data-mcp
 fd-akshare/yfinance/world/             concept_bindings   (column -> concept)
 cn-report/cn-gov registries            entity_source_identifiers (per-source id)
 fd-entities-indicators                 source_rankings    (quality × access × freshness)
   indicator_defs (926 concepts)        semantic_observations (read-through cache)
   countries/cities/symbols/sw_industries   fetch_log / schedules / executions
        │
   TRANSFORMERS: import_catalog, consume_concepts, propose_bindings,
                 seed_entity_identifiers, generate_refresh_schedules
        │
   RUNTIME: read() -> cache hit? : dispatch (ranked, failover) -> cache -> log
```

Six capabilities (see `openspec/changes/add-fd-open-data-mcp/specs/`):
`open-data-catalog`, `semantic-layer`, `entity-identity`, `source-ranking`,
`concept-fetch`, `scheduled-refresh`.

## Install

```bash
cd /Users/chengsishi/finddata/fd-open-data-mcp
uv sync                  # base install

# For full data source support (akshare, yfinance, edgar, world bank, etc.)
uv sync --extra data     
```

The DB path defaults to `fd_open_data_mcp/metadata/daas.db`; override with
`FD_OPEN_DATA_MCP_DATABASE_URL`. `FINDDATA_ROOT` (default: the parent
`finddata/` dir) locates the `fd-*` providers. 

**Note**: Before using SEC EDGAR data, set `EDGAR_IDENTITY="your_email@example.com"` in your environment.

## Quickstart

```bash
# 1. create the ontology tables
fd-open-data-mcp migrate

# 2. import the catalogs (akshare 673, yfinance 12, cn-gov 11, cn-report 44, edgar 6, ...)
fd-open-data-mcp import-catalog
# or one provider:  fd-open-data-mcp import-catalog akshare

# 3. consume the 926 indicator_defs as concepts + propose column->concept bindings
fd-open-data-mcp consume-concepts
fd-open-data-mcp propose-bindings

# 4. seed per-source entity identifiers (akshare/yfinance for stocks, worldbank for countries)
fd-open-data-mcp seed-entities

# 5. generate per-concept refresh schedules from indicator_defs.frequency
fd-open-data-mcp generate-schedules

# 6. read data by concept + entity (read-through cache + ranked dispatch + failover)
fd-open-data-mcp read --concept-id 234 --entity-type stock --entity-id 1 --date 2024-07-26
```

## MCP server

```bash
fd-open-data-mcp serve          # FastMCP, stdio transport
```

16 tools: `import_catalog`, `consume_concepts`, `propose_bindings`,
`list_concepts`, `list_bindings`, `review_bindings`, `confirm_binding`,
`seed_entity_identifiers`, `resolve_entity`, `add_entity_identifier`,
`rank_sources`, `read`, `fetch`, `generate_refresh_schedules`,
`list_schedules`, `run_schedule`.

## Crawl control center (panel + reconciler)

Policies describe *what to crawl*: concepts × entity scope × date range ×
frequency × mode. A `CrawlPolicy` is created from the panel, compiled by the
reconciler into a `CrawlPlan`, and executed by `scraw-fd-open-data-mcp` into
`semantic_observations`.

```bash
# Serve the control panel (default http://0.0.0.0:8000)
FD_OPEN_DATA_MCP_DATABASE_URL=<db url> fd-open-data-mcp panel

# Run the reconciler once (due policies -> launch; closes stale runs)
python -m fd_open_data_mcp.refresh.reconciler
```

**Env vars:**
- `PANEL_TOKEN` — if set, `/panel/*` requires it (header `X-Panel-Token`,
  `?token=`, or cookie).
- `POLICY_MAX_FETCHES` (default `50000`) — plan-size guardrail; a due policy
  whose fetch estimate exceeds it is refused (recorded as a failed run) unless
  the policy has `force` set.
- `RECONCILER_LAUNCHER` — `scrapyd` (default) or `k8s` (`K8sJobLauncher`).
- `SCRAPYD_URL` / `SCRAW_PLAN_DIR` (scrapyd launcher), `SCRAW_K8S_NAMESPACE` /
  `SCRAW_K8S_IMAGE` / `SCRAW_K8S_DATABASE_URL` / `SCRAW_K8S_REDIS_URL`
  (k8s launcher).
- `FD_PROXY_POOL=off` — local dev: bypass the cluster proxy pool (its free
  proxies break akshare/eastmoney).

**Policy example** (via panel, or MCP `policy_create`):

```
name:        fund-nav-daily
entity_type: fund
concepts:    nav.unit, nav.accumulated
mode:        per_date          # or "series" (one bulk fetch per entity)
date_policy: since_last        # start = observation watermarks
frequency:   daily
source:      akshare
cron:        45 6 * * * UTC
```

Two cadence notes: `series` mode backfills history in one bulk fetch per entity
(explicit range), while `since_last` `per_date` is the steady-state incremental
mode (only new dates since each concept's watermark; entities with no watermark
are not backfilled — run an explicit-range backfill first). See
`openspec/changes/add-fund-crawl-control-center/docs/phase7-validation.md` for
the validated pilot (76k nav observations on the live DB).

## Tests

```bash
uv run --with pytest pytest -q
```

## Design notes / v1 limitations

- **Propose-and-confirm**: column->concept bindings carry `confidence` +
  `provenance`; below-threshold bindings are withheld from dispatch (review
  queue). A real fetch promotes a binding to `sample-confirmed`.
- **Ranking** is per `(source × concept)`, self-tuning from `fetch_log`
  (bounded so one failure can't remove a source).
- **Conflict policy**: one cached value per `(concept, entity, date)` with
  `source_used` attached; values are never merged across sources.
- **LLM provider** for meaning-enrichment / cross-language concept mapping is
  an open question (`design.md`); v1 uses a rule table + `semantic_type` hints.
- `_build_params` / `_extract_value` in the fetch runner are best-effort; a
  production runner refines per-function date-format / payload-shape quirks.

See `openspec/changes/add-fd-open-data-mcp/` for the full spec.

---

## Data Sources

All **19 data sources** are now fully integrated and accessible through the unified API. Use `fd-open-data-mcp list-sources` to see the complete catalog.

### Fully Integrated (✅)

| Data Source | Description | Example Commands |
|------------|-------------|------------------|
| **akshare** | A-share stocks, funds, financial data | `stock_zh_a_daily`, `fund_etf_funddaily_return` |
| **yfinance** | Yahoo Finance global stocks | `ticker_<method>`, `download` |
| **cn-report** | China financial reports (26 tools) | `extract_financial_indicators`, `extract_balance_sheet` |
| **nbs-gdp** | National Bureau of Statistics GDP | `get_gdp_quarterly`, `get_gdp_annual` |
| **cisa-industry** | Ministry of Industry industry stats | `get_steel_production`, `get Cement_output` |
| **amac-fund** | AMAC fund management data | `get_fund_info`, `get_manager_info` |
| **shfe-metal-futures** | Shanghai Futures Exchange metals | `get_metal_pricing`, `get_futures_volume` |
| **agriculture** | DCE agricultural futures | `get_agricultural_prices`, `get_futures_open_interest` |
| **cme-agricultural-futures** | CME agricultural futures | `get_corn_prices`, `get_soybean_data` |
| **chemicals** | Chemical industry prices & PMI | `get_chemical_prices`, `get_industry_index` |
| **electronics** | Electronics industry association | `get_semiconductor_stats`, `get_industry_output` |
| **nonferrous** | Non-ferrous metals industry | `get_aluminum_prices`, `get_lithium_data` |
| **flowers-kifc** | Kunming flower auction center | `get_daily_prices`, `get_volume_stats` |
| **fin_platforms** | Wind financial terminal | `get_market_benchmark`, `get_sector_performance` |
| **sac-securities** | Securities association statistics | `get_trading_stats` |
| **edgar** | SEC EDGAR filings (requires env var) | `company_<method>`, requires `EDGAR_IDENTITY` |
| **wbgapi** | World Bank data API | `get_indicator_data`, `list_economies` |

### Partial Support (⚠️)

| Data Source | Status | Notes |
|------------|--------|-------|
| **cn-gov** | Read-only registry | Government open information (manifest-based) |
| **world** | Read-only catalog | CKAN + Chinese NBS Statistics |

To view detailed status:
```bash
fd-open-data-mcp list-sources
```

---

### Usage Examples

#### Query NBS GDP Data
```python
from fd_open_data_mcp.fetch.runner import run_upstream

result = run_upstream(
    source='nbs-gdp', 
    command='get_gdp_quarterly',
    params={'start_year': 2020}
)
print(result.head())
```

#### Query Steel Industry Production
```python
result = run_upstream(
    source='cisa-industry',
    command='get_steel_production',
    params={}
)
print(result.head())
```

#### Query Metal Futures Pricing
```python
result = run_upstream(
    source='shfe-metal-futures',
    command='get_metal_pricing',
    params={}
)
print(result.head())
```

### CLI Usage
```bash
# List all data sources
fd-open-data-mcp list-sources

# Read specific data
fd-open-data-mcp read \
  --source nbs-gdp \
  --function get_gdp_quarterly \
  --params '{"start_year": 2020}'
```

### MCP Server Mode
```bash
uv run fd-open-data-mcp serve
# Then connect from Claude/Codex/etc.
```


---

## Rate Limits & Best Practices

### Recommended Refresh Intervals

| Data Source Type | Refresh Interval | Notes |
|------------------|------------------|-------|
| **GDP/Macro** | Weekly | Stable data, updates quarterly/monthly |
| **Industry Stats** | Daily | Can change frequently |
| **Futures Prices** | Hourly during market hours | Volatile pricing |
| **Fund Statistics** | Monthly | Updates monthly |
| **Market Indices** | Real-time | High volatility |

### API Rate Limiting

- **Government APIs**: Respect 10 requests/minute default limits
- **Exchange APIs**: Follow exchange-specific rate policies  
- **Third-party Data**: Check individual terms of service
- **Recommendation**: Implement exponential backoff on 429 errors

### Caching Strategy

All fetch results are automatically cached based on data frequency:
- High-frequency data (futures): Cache for 1 hour
- Medium-frequency data (industry stats): Cache for 24 hours
- Low-frequency data (GDP, annual reports): Cache for 1 week

Use `fd-open-data-mcp read` to check cache status.

---

## Troubleshooting

### Common Issues

#### 1. "EDGAR_IDENTITY env var is not set"

**Problem**: Cannot access SEC EDGAR data.

**Solution**: Set the required identity before running:
```bash
export EDGAR_IDENTITY="your_email@example.com"
```

Also ensure you installed with the `data` extra:
```bash
uv sync --extra data     # installs edgartools, akshare, yfinance, wbgapi, etc.
```

The SEC requires a valid User-Agent identity for anonymous access.

#### 2. "no runner for source {source}"

**Problem**: Trying to use an unsupported data source.

**Solution**: 
1. Check available sources: `fd-open-data-mcp list-sources`
2. Ensure you're using one of the supported sources listed in the table above
3. For custom sources, register them using `fd-open-data-mcp register-datasource <path>`

#### 3. Data fetch returns empty or stale data

**Possible causes:**
- **Rate limiting**: Some APIs have strict rate limits (especially government APIs)
- **Cache still valid**: The cached data hasn't expired yet
- **Upstream API change**: The source's API may have changed

**Solutions:**
- Wait a few minutes and retry (exponential backoff recommended)
- Force refresh: Use MCP `fetch` tool instead of `read` to bypass cache
- Update adapter: If upstream API changed, update the corresponding adapter file

#### 4. Adapter returns unexpected columns

**Problem**: The data doesn't match expected schema.

**Solution:**
- Run `fd-open-data-mcp propose-bindings` to re-propose column->concept bindings
- Review the returned columns with `fd-open-data-mcp list-bindings --concept-id <id>`
- Confirm correct bindings manually if needed

#### 5. Proxy connection failures

**Problem**: Sources report "sources_all_proxies_open" alert.

**Solution:**
```bash
# Seed fresh proxy health data
fd-open-data-mcp seed-proxy-health

# Run probe cycle to test all proxies
fd-open-data-mcp probe-cycle

# Check current proxy status
fd-open-data-mcp proxy-health
```

### Getting Help

If you encounter issues not covered here:

1. **Check logs**: Review output from `proxy-health` command
2. **Verify setup**: Ensure all initialization steps completed successfully (`migrate`, `import-catalog`, etc.)
3. **Test individually**: Try calling adapters directly via Python to isolate the issue
4. **File an issue**: Open an issue on the GitHub repository with error details

---

## Contributing

To add support for a new data source:

1. Create an adapter file in `fd_open_data_mcp/adapters/<source>.py`
2. Implement `run_<source>(command, params)` function
3. Add routing in `fetch/runner.py`'s `run_upstream()` function  
4. Test with `pytest tests/test_adapters.py`
5. Document in README.md under the "Fully Integrated" section

See `openspec/changes/complete-all-datasource-support/specs/adapter-template/spec.md` for detailed adapter requirements.

## LLM Configuration (for PDF Report Extraction)

`fd-cn-report` uses LLM to extract financial indicators from PDF annual reports. Configure your LLM provider in `.env.local`:

### OpenAI (Default)

```bash
# .env.local
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=sk-your-openai-api-key
LLM_MODEL=gpt-4o
```

### Azure OpenAI

```bash
# .env.local
LLM_BASE_URL=https://YOUR_RESOURCE.openai.azure.com/openai/deployments/YOUR_DEPLOYMENT
LLM_API_KEY=your-azure-api-key
LLM_MODEL=gpt-4o
```

### Local LLM (Ollama)

```bash
# .env.local
LLM_BASE_URL=http://localhost:11434/v1
LLM_API_KEY=ollama  # any value works for local
LLM_MODEL=llama3.1
```

### OpenRouter

```bash
# .env.local
LLM_BASE_URL=https://openrouter.ai/api/v1
LLM_API_KEY=sk-or-your-openrouter-key
LLM_MODEL=anthropic/claude-3.5-sonnet
```

**Note**: The system supports both `LLM_API_KEY` and `OPENAI_API_KEY` environment variables for backward compatibility. `LLM_API_KEY` takes priority if both are set.


## Web Scraping with Playwright

Some data sources require JavaScript rendering. `fd-open-data-mcp` includes Playwright for web scraping.

### Installation

```bash
# Install with data extras (includes Playwright)
uv sync --extra data

# Install browsers
playwright install chromium
```

### Configuration

Configure Playwright in `.env.local`:

```bash
# Browser settings
PLAYWRIGHT_BROWSER=chromium
PLAYWRIGHT_HEADLESS=true
PLAYWRIGHT_TIMEOUT=30000

# Viewport
PLAYWRIGHT_VIEWPORT_WIDTH=1920
PLAYWRIGHT_VIEWPORT_HEIGHT=1080

# Optional: Custom user agent
PLAYWRIGHT_USER_AGENT=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36
```

### Usage

```python
from fd_open_data_mcp.scraping import scrape_page, scrape_with_selector

# Scrape a page (waits for JavaScript to load)
html = scrape_page("https://example.com", wait_for="table.data")

# Extract specific elements
links = scrape_with_selector(
    "https://example.com",
    "a.article-link",
    attribute="href"
)
```

### Supported Data Sources

Playwright is used for:
- **cisa-industry**: China Iron and Steel Association data
- **Other industry associations**: When APIs are not available
- **Government websites**: With JavaScript-rendered content

