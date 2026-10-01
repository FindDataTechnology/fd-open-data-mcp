# fd-open-data-mcp

> **Wire (柏讯) product line** · the open-data supply line of [FindData](https://www.finddatatech.cloud/products/wire) — 柏讯线开源核心：开放数据本体与语义供给。

**English** | [中文](README.zh-CN.md)

An **open-data ontology MCP**: a semantic concept layer over multi-datasource
financial/economic data. You ask for data by **concept + entity** (e.g. "price.close
of Moutai", "GDP of China"); the system resolves concepts to physical columns in
each source, ranks candidate sources by quality and reachability, fetches from
the best one (with failover), caches per concept, and refreshes on a
per-concept schedule.

It consumes finddata's `fd-*` datasource registry and `fd-entities-indicators`
**read-only**, and adds the unified layer on top.

## One-command install

A single self-contained block that bootstraps the whole finddata open-data stack
(hub + all datasource packages + the ontology database). Re-runnable; stops at
the first error.

```bash
# 1) Install the full stack from PyPI.
#    fd-open-data-protocol comes in transitively; fd-polygon and fd-cn-report
#    self-register via entry points. Drop "[data]" for a light install (MCP
#    server + CLI only, no akshare/yfinance SDKs). "[data]" excludes the
#    browser-rendering stack; add it via "[data,browser]" when
#    scrapling/playwright is needed.
pip install "fd-open-data-mcp[data]" fd-polygon fd-cn-report

# 2) Initialize the ontology DB and wire every layer: catalog -> concepts
#    -> column bindings -> per-source entity ids -> refresh schedules
#    -> manifest.
fd-open-data-mcp migrate \
  && fd-open-data-mcp import-catalog \
  && fd-open-data-mcp consume-concepts \
  && fd-open-data-mcp propose-bindings \
  && fd-open-data-mcp seed-entities \
  && fd-open-data-mcp generate-schedules \
  && fd-open-data-mcp register-discovered

# 3) Start the MCP server (stdio transport, for any MCP client).
fd-open-data-mcp serve
```

Real-data fetching needs datasource keys in the environment (never commit
them): `POLYGON_API_KEY`, `EDGAR_IDENTITY`, plus the `LLM_*` / `ES_*` vars
required by `fd-cn-report`. See each package's configuration section.

## Architecture

```
Consumed (read-only)                 Added by fd-open-data-mcp
 fd-akshare/yfinance/world/           concept_bindings     (column -> concept)
 cn-report/cn-gov/polygon/            entity_source_identifiers (per-source ids)
 datacommons                          source_rankings     (quality x reach x freshness)
 fd-entities-indicators               semantic_observations (read-through cache)
   indicator_defs (926 concepts)      fetch_log / schedules / executions / policies
   countries/cities/symbols/sw_industries   entities / relationships (graph)
        │
   Loaders: import_catalog, consume_concepts, propose_bindings,
            seed_entity_identifiers, generate_refresh_schedules
        │
   Runtime: read() -> cache hit? : dispatch (ranked + failover) -> cache -> log
   Search : semantic_search (concepts) + graph_search (entity graph) + ai_search
```

Eight capabilities (see `openspec/changes/add-fd-open-data-mcp/specs/`):
`open-data-catalog`, `semantic-layer`, `entity-identity`, `source-ranking`,
`concept-fetch`, `scheduled-refresh`, `entity-graph`, `vector-search`.

## Install

```bash
cd fd-open-data-mcp
uv sync                  # base install

# full datasource support (akshare, yfinance, edgar, world bank, ...)
uv sync --extra data
```

The database path defaults to `fd_open_data_mcp/metadata/daas.db`, overridable
via `FD_OPEN_DATA_MCP_DATABASE_URL`. `FINDDATA_ROOT` (default: the parent
`finddata/` directory) locates `fd-*` providers.

**Note**: before using SEC EDGAR data, set
`EDGAR_IDENTITY="your_email@example.com"` in the environment.

## Quick start

```bash
# 1. Create the ontology tables
fd-open-data-mcp migrate

# 2. Import catalogs (akshare 673, yfinance 12, cn-gov 11, cn-report 44, edgar 6, ...)
fd-open-data-mcp import-catalog
# or a single provider: fd-open-data-mcp import-catalog akshare

# 3. Consume the 926 indicator_defs as concepts and propose column->concept bindings
fd-open-data-mcp consume-concepts
fd-open-data-mcp propose-bindings

# 4. Seed per-source entity identifiers (stocks for akshare/yfinance, countries for worldbank)
fd-open-data-mcp seed-entities

# 5. Generate refresh schedules from indicator_defs.frequency
fd-open-data-mcp generate-schedules

# 6. Read by concept + entity (read-through cache + ranked dispatch + failover)
fd-open-data-mcp read --concept-id 234 --entity-type stock --entity-id 1 --date 2024-07-26
```

## MCP server

```bash
fd-open-data-mcp serve          # FastMCP, stdio transport
```

**36 tools**, registered across five files:

| Group | Tools |
|-------|-------|
| Catalog/bindings | `import_catalog`, `consume_concepts`, `propose_bindings`, `list_bindings`, `review_bindings`, `confirm_binding`, `update_binding`, `list_concepts`, `list_cnreport_rules`, `enumerate_wbgapi_indicators`, `register_datasource`, `register_discovered` |
| Entity identity | `seed_entity_identifiers`, `resolve_entity`, `add_entity`, `update_entity`, `get_entity`, `list_entities`, `add_entity_identifier`, `ingest_entities_from_dump` |
| Ranking/read | `rank_sources`, `read`, `fetch` |
| Refresh schedules | `generate_refresh_schedules`, `list_schedules`, `run_schedule`, `plan_crawl` |
| Entity graph | `add_relationship`, `list_relationships`, `get_entity`, `graph_search` |
| Vector/semantic search | `semantic_search`, `semantic_search_entities`, `semantic_search_unified`, `ai_search`, `re_embed_concept`, `update_concept` |

## Catalog expansion and the read boundary

The `list_concepts` family (MCP tools and the `list-concepts` CLI) also returns
entries from the **unified indicator registry** (`registry_entries` table) that
passed the naming-quality gate (verified), covering world_bank / gta_panel /
china_city_panel / the fd_open_data mirror; yearbook's numeric-placeholder
entries stay unverified and out of the catalog.

- **Merge semantics**: local concepts (`concepts` table) first, registry entries
  after (ordered by domain, semantic_code); entries whose semantic_code
  duplicates a local concept code are deduped (local row wins). Registry rows
  carry `native_code` and `source_db` identity fields, so retrieval works with
  either the native or the semantic code (the `query` parameter substring-matches
  code/name). Without that table in the local DB, the tool falls back to the
  pure local catalog — behavior unchanged.
- **Read boundary (important)**: this server does **not** read observation data
  for those external-registry indicators — forwarding reads through
  business-mcp is a later change; in this version `read`/`fetch` on registry
  indicators does not return external-source data. This expansion affects the
  indicator dictionary only, not any data-reading capability.

## Data sources

The dispatcher `run_upstream()` routes by source name to adapters. The table
below is ordered by **verified reachability**, not self-reported status.

### Verified online ✅

| Source | Notes |
|--------|-------|
| **akshare** | A-share stocks/funds/financials |
| **yfinance** | Yahoo Finance global equities |
| **cn-report** | Chinese financial reports (44 tools, see [fd-cn-report](https://github.com/FindDataTechnology/fd-cn-report)) |
| **edgar** | SEC EDGAR filings (needs `EDGAR_IDENTITY`) |
| **wbgapi** | World Bank data API |
| **cnstats** | National Bureau of Statistics data |
| **ckan** | CKAN catalog data |
| **nbs-gdp** | NBS GDP data |
| **datacommons** | Google Data Commons (needs `DC_API_KEY`) |
| **polygon** | Polygon.io US equities (external package fd-polygon) |
| **edinet** | Japan EDINET filings |
| **dartlab** | Korea DARTLab disclosures |

> The `polygon` and `datacommons` runners live in external `fd-*` packages,
> lazily loaded via the manifest's `fetch.module` — `fd-open-data-mcp` does not
> depend on `polygon-api-client` / `requests` unless a fetch actually happens.

### Stub adapters ⚠️ (sample data only, **no** network)

| Source | Status |
|--------|--------|
| **cisa-industry** | stub — static sample rows |
| **amac-fund** | stub |
| **shfe-metal-futures** | stub |
| **agriculture** | stub (DCE) |
| **cme-agricultural-futures** | stub (CME) |
| **chemicals** | stub |
| **electronics** | stub |
| **nonferrous** | stub |
| **flowers-kifc** | stub |
| **fin_platforms** | stub (Wind) |
| **sac-securities** | stub |

These adapters expose `run_<source>()` entries and manifest registrations, so
`list-sources` self-reports "✅ Full support" — but they return synthetic
sample rows, not real exchange or association data. Read the adapter file
before depending on any of them.

### Read-only catalogs

| Source | Status |
|--------|--------|
| **cn-gov** | manifest registration only (11 ministry catalogs) |
| **world** | CKAN + Chinese NBS statistical catalogs |

## Crawl control center (panel + reconciler)

A policy describes **what to crawl**: concept × entity scope × date range ×
frequency × mode. `CrawlPolicy` objects are created on the panel, compiled into
`CrawlPlan`s by the reconciler, and executed by `scraw-fd-open-data-mcp` writing
into `semantic_observations`.

```bash
# Start the control panel (default http://0.0.0.0:8000)
FD_OPEN_DATA_MCP_DATABASE_URL=<db url> fd-open-data-mcp panel

# Run the reconciler once (due policies -> launch; close stale runs)
python -m fd_open_data_mcp.refresh.reconciler
```

**Environment variables:**
- `PANEL_TOKEN` — when set, `/panel/*` requires it (header `X-Panel-Token`,
  `?token=`, or cookie).
- `POLICY_MAX_FETCHES` (default `50000`) — plan-size guardrail; a due policy
  whose fetch estimate exceeds it is rejected (recorded as a failed run)
  unless the policy sets `force`.
- `RECONCILER_LAUNCHER` — `scrapyd` (default) or `k8s` (`K8sJobLauncher`).
- `SCRAPYD_URL` / `SCRAW_PLAN_DIR` (scrapyd launcher); `SCRAW_K8S_NAMESPACE` /
  `SCRAW_K8s_IMAGE` / `SCRAW_K8S_DATABASE_URL` / `SCRAW_K8S_REDIS_URL`
  (k8s launcher).
- `FD_PROXY_FORWARDER` — empty for local dev (the injection layer returns a
  direct sentinel → direct egress); cluster crawling uses the standalone
  `fd-proxy-service` forwarder for proxy selection. The legacy vars
  `FD_PROXY_POOL`/`FD_EGRESS_MODE` are no longer read.

## Tests

```bash
uv run --with pytest pytest -q
```

## Design notes / v1 limitations

- **Propose-confirm flow**: column→concept bindings carry `confidence` +
  `provenance`; below-threshold bindings stay out of dispatch (into the review
  queue). One real fetch promotes a binding to `sample-confirmed`.
- **Ranking** is per `(source × concept)`, self-tuning from `fetch_log`
  (bounded — one failure never removes a source).
- **Conflict policy**: one cached value per `(concept, entity, date)` with its
  `source_used`; cross-source values are never merged.
- **LLM providers** power semantic enrichment / cross-language concept
  mapping. v1 uses rule tables + `semantic_type` hints.

Full specifications: `openspec/changes/add-fd-open-data-mcp/`.

## Proxy pool & circuit breaker

The fetch stack rotates IPs through a **proxy pool** to avoid source bans, with
per-(source, real_source×proxy_ip) circuit breakers. This infrastructure is
deployed in the `scraw` namespace of the remote k8s cluster.

```bash
# Manually trigger a proxy sync
kubectl create job --from=cronjob/proxy-pool-sync proxy-pool-sync-manual -n scraw

# Check proxy pool health
kubectl exec -n scraw fd-open-pg-789d56dbb5-fkdbl -- \
  psql -U postgres -d postgres -c "SELECT status, count(*) FROM proxies GROUP BY status;"
```

Key files: `fd_open_data_mcp/proxy/` (selector, breaker, ban rules, injection);
real-source failover in `fd_open_data_mcp/fetch/dispatch.py`.

## Standard real-source names

- `eastmoney` — Eastmoney (A-share primary, via akshare)
- `tencent` — Tencent Finance (A-share failover)
- `sina` — Sina Finance (A-share alternative)
- `yahoo_finance` — Yahoo Finance (global markets, via yfinance)

Libraries (`akshare`, `yfinance`) call several real sources underneath; the
breaker tracks health per real source, not per library, enabling smart
failover.

## LLM configuration (PDF report extraction)

`fd-cn-report` uses an LLM to extract financial indicators from annual-report
PDFs. The current provider is **DeepSeek on Ark** (see the
[fd-cn-report](https://github.com/FindDataTechnology/fd-cn-report) README):

```bash
# fd-open-data-mcp/.env
LLM_BASE_URL=…/api/plan/v1     # Ark endpoint
LLM_API_KEY=…                  # Ark key
LLM_MODEL=deepseek-v4-flash
```

`LLM_API_KEY` and `OPENAI_API_KEY` (legacy) are both accepted;
`LLM_API_KEY` wins when both are set.

## Image publish

Images are built by GitHub Actions (`.github/workflows/image.yml`) and pushed to
Tencent TCR personal edition: `ccr.ccs.tencentyun.com/finddata/fd-open-data-mcp:sha-<short>`
(+ rolling `main`). Release ritual: dev pushes go to gitee; publishing =
`git push github main`. The Jenkins→Harbor path is the fallback channel only.

## License

MIT
