# Configuration Guide

This document explains all configuration options for `fd-open-data-mcp`.

## Environment Files

The system loads configuration from multiple sources (in priority order):

1. **`.env.local`** - Your personal configuration (DO NOT COMMIT)
2. **`.env`** - Default configuration template
3. **Environment variables** - System-level overrides

## Configuration Categories

### 1. Database Configuration

```bash
# SQLite database path (default)
FD_OPEN_DATA_MCP_DATABASE_URL=sqlite:///metadata/daas.db

# PostgreSQL example
FD_OPEN_DATA_MCP_DATABASE_URL=postgresql://user:pass@localhost:5432/fd_open_data
```

### 2. FindData Workspace

```bash
# Root directory of the finddata workspace
FINDDATA_ROOT=/Users/chengsishi/finddata
```

### 3. SEC EDGAR Identity

```bash
# Required for accessing SEC EDGAR data
# Format: email or "Name <email>"
EDGAR_IDENTITY=your_email@example.com
```

### 4. Financial Data Platform API Keys

```bash
# Wind Financial Terminal (requires license)
WIND_API_KEY=

# iFinD Data Platform
IFIND_API_KEY=

# Tongdaxin Data
TONGDAOXIN_API_KEY=
```

### 5. LLM Configuration (for PDF Report Extraction)

`fd-cn-report` uses LLM to extract financial indicators from PDF annual reports.

#### OpenAI (Recommended)

```bash
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=sk-your-openai-api-key
LLM_MODEL=gpt-4o
```

#### Azure OpenAI

```bash
LLM_BASE_URL=https://YOUR_RESOURCE.openai.azure.com/openai/deployments/YOUR_DEPLOYMENT
LLM_API_KEY=your-azure-api-key
LLM_MODEL=gpt-4o
```

#### Local LLM (Ollama)

```bash
LLM_BASE_URL=http://localhost:11434/v1
LLM_API_KEY=ollama
LLM_MODEL=llama3.1
```

#### OpenRouter

```bash
LLM_BASE_URL=https://openrouter.ai/api/v1
LLM_API_KEY=sk-or-your-openrouter-key
LLM_MODEL=anthropic/claude-3.5-sonnet
```

**Note**: Both `LLM_API_KEY` and `OPENAI_API_KEY` are supported. `LLM_API_KEY` takes priority.

### 6. Proxy Configuration

```bash
# Enable/disable proxy
PROXY_ENABLED=false

# Proxy URLs (comma-separated)
PROXY_URLS=http://proxy1:8080,http://proxy2:8080
```

### 7. Rate Limiting

```bash
# Requests per minute (default: 60)
RATE_LIMIT_REQUESTS_PER_MINUTE=60
```

### 8. Logging

```bash
# Log level: DEBUG, INFO, WARNING, ERROR, CRITICAL
LOG_LEVEL=INFO
```

### 9. Cache Settings

```bash
# Enable/disable caching
CACHE_ENABLED=true

# Cache TTL in hours (default: 24)
CACHE_TTL_HOURS=24
```

### 10. Development Settings

```bash
# Enable development mode (DO NOT use in production)
DEV_MODE=false
```

## Quick Setup

1. Copy `.env.example` to `.env.local`:
   ```bash
   cp .env.example .env.local
   ```

2. Edit `.env.local` with your values:
   ```bash
   nano .env.local
   ```

3. Verify configuration:
   ```bash
   uv run python -c "import os; print(os.environ.get('EDGAR_IDENTITY'))"
   ```

## Security Best Practices

- ✅ **Never commit `.env.local` to Git** (it's in `.gitignore`)
- ✅ **Rotate API keys regularly**
- ✅ **Use environment-specific keys** (dev/staging/prod)
- ✅ **Restrict file permissions**: `chmod 600 .env.local`
- ✅ **Use secrets management** in production (AWS Secrets Manager, etc.)

## Troubleshooting

### LLM Not Configured

**Error**: `LLM_API_KEY is not configured`

**Solution**:
```bash
echo 'LLM_API_KEY=your_key_here' >> .env.local
```

### EDGAR Identity Missing

**Error**: `EDGAR_IDENTITY env var is not set`

**Solution**:
```bash
echo 'EDGAR_IDENTITY=your_email@example.com' >> .env.local
```

### Database Connection Failed

**Check**:
1. Database URL is correct
2. Database file/directory exists and is writable
3. For PostgreSQL: credentials and network access

## Configuration Precedence

The system loads configuration in this order (highest to lowest priority):

1. Command-line arguments
2. Environment variables (exported in shell)
3. `.env.local` file
4. `.env` file
5. Default values in code

This means you can override any setting at runtime:

```bash
# Override database URL for one command
FD_OPEN_DATA_MCP_DATABASE_URL=sqlite:///test.db fd-open-data-mcp migrate
```

### 11. Crawl control center (panel + reconciler)

```bash
# Control-panel auth gate (if set, /panel/* requires it — including the
# observability home, its /panel/partials/* polling endpoints, /panel/data
# and /panel/runs/{id})
PANEL_TOKEN=

# Plan-size guardrail: a due policy whose fetch estimate exceeds this is
# refused unless the policy has force=true (recorded as a failed PolicyRun)
POLICY_MAX_FETCHES=50000

# Launcher: "scrapyd" (local, default) or "k8s" (K8sJobLauncher)
RECONCILER_LAUNCHER=scrapyd

# scrapyd launcher
SCRAPYD_URL=http://localhost:6800
SCRAW_PLAN_DIR=plans

# k8s launcher
SCRAW_K8S_NAMESPACE=scraw
SCRAW_K8S_IMAGE=harbor.local/lawcraw_business/scraw-fd-open-data-mcp
SCRAW_K8S_DATABASE_URL=postgresql://...
SCRAW_K8S_REDIS_URL=redis://...

# Local dev: leave FD_PROXY_FORWARDER unset — the injection shim returns a
# direct sentinel (direct egress, no pool). The standalone fd-proxy-service
# forwarder owns proxy selection in cluster crawls. The legacy
# FD_PROXY_POOL / FD_EGRESS_MODE vars are no longer read.
#FD_PROXY_FORWARDER=
```

### 12. Playwright Configuration (Web Scraping)

For websites that require JavaScript rendering, configure Playwright:

```bash
# Browser type: chromium, firefox, webkit
PLAYWRIGHT_BROWSER=chromium

# Headless mode (true = background, false = visible browser)
PLAYWRIGHT_HEADLESS=true

# Timeout in milliseconds (default: 30000 = 30 seconds)
PLAYWRIGHT_TIMEOUT=30000

# Slow down operations (for debugging)
PLAYWRIGHT_SLOW_MO=0

# Viewport size
PLAYWRIGHT_VIEWPORT_WIDTH=1920
PLAYWRIGHT_VIEWPORT_HEIGHT=1080

# Custom user agent (optional)
PLAYWRIGHT_USER_AGENT=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36

# Proxy configuration (optional)
PLAYWRIGHT_PROXY_SERVER=http://proxy.example.com:8080
PLAYWRIGHT_PROXY_USERNAME=
PLAYWRIGHT_PROXY_PASSWORD=

# JavaScript and security settings
PLAYWRIGHT_JAVASCRIPT_ENABLED=true
PLAYWRIGHT_IGNORE_HTTPS_ERRORS=false
```

**Install browsers:**
```bash
# After installing the package
playwright install chromium

# Or install all browsers
playwright install
```

**Usage example:**
```python
from fd_open_data_mcp.scraping import scrape_page, scrape_with_selector

# Simple page scrape
html = scrape_page("https://example.com", wait_for="table.data")

# Extract specific elements
links = scrape_with_selector(
    "https://example.com",
    "a.article-link",
    attribute="href"
)
```


### 13. Federated registry reads (registry-transparent-read-and-scope)

Registry-only indicators (catalog rows with `id=None`) are read by routing
to the business-mcp domain read tools over MCP HTTP. Off by default — the
a→b service dependency goes live deliberately (flag defaults closed, staged
per-domain rollout):

```bash
# business-mcp Streamable HTTP endpoint (required for federated reads)
FD_MCP_BUSINESS_URL=http://<business-mcp-host>:8310/mcp

# Rollout flag — unset/0 = off (default); 1/all = every mapped source;
# or a comma list for the staged rollout:
FD_MCP_FEDERATED_READ=yearbook_catalog          # yearbook first…
FD_MCP_FEDERATED_READ=1                          # …then all mapped domains

# Per-call timeout (seconds) and the post-failure retry cooldown (seconds —
# a down business-mcp is not re-dialed per date within the window)
FD_MCP_FEDERATION_TIMEOUT=20
FD_MCP_FEDERATION_COOLDOWN=30
```

Mapped channels: `yearbook_catalog → yearbook_read`,
`world_bank → wb_read`, `gta_panel → gta_read`,
`china_city_panel → city_read`. Sources without a channel report
`no_read_channel`; an unreachable business-mcp reports
`federation_unavailable` — both explicit, never empty values. Local concept
reads never touch this path.

### 14. Retrieval scopes (indicator-scope)

Named allow-list filters over `{source_dbs, domains, semantic_codes,
native_codes}`, stored in `fd_open_data` (`scopes` / `scope_bindings` /
`scope_stats` — migration `0004_scope_tables`). Managed via the
`scope_create/list/update/delete` MCP tools; consumed by the optional
`scope` parameter on `ai_search` / `semantic_search` /
`semantic_search_unified` / `read` / `read_series` and on business-mcp's
domain search tools.

```bash
# Caller identity for default-scope bindings (opaque string; the deployment
# decides granularity). HTTP callers may alternatively send X-FD-Caller.
FD_MCP_CALLER_KEY=tok-report-bot
```

- Explicit `scope` parameter always wins over the caller default; the
  reserved name `unscoped` forces the full corpus.
- Every scoped response carries `scope: {name, summary}` — scoped emptiness
  is distinguishable from true absence.
- `scope_stats(scope_name)` returns per-day `calls` / `results_returned`.
