# fd-open-data-mcp

An **open-data ontology MCP**: a semantic concept layer over multi-datasource
financial/economic data. You ask for data in **concepts + entities** (e.g.
"price.close for Moutai", "GDP for China"); the system resolves the concept to
physical columns across datasources, ranks candidate sources by quality +
accessibility, fetches from the best one (with failover), caches by concept,
and refreshes on a per-concept frequency.

It consumes the finddata `fd-*` datasource registries and
`fd-entities-indicators` **read-only** and adds the unifying layers on top:
concept bindings, per-source entity identifiers, source rankings, a
read-through value cache, and (on top of all that) an entity graph +
vector-search layer for relational and semantic queries.

**English** | [中文](README.zh-CN.md)

一个**开放数据本体 MCP**：在多数据源的金融/经济数据之上构建语义概念层。你用**概念 + 实体**来请求数据（例如"茅台的 price.close"、"中国的 GDP"）；系统将概念解析为各数据源中的物理列，按质量 + 可达性对候选数据源排序，从最佳数据源抓取（带故障转移），按概念缓存，并按每个概念的频率刷新。

## One-click install

A single self-contained block that bootstraps the entire finddata open-data
stack (hub + every datasource package + ontology DB). Safe to re-run; stops on
the first error.

```bash
# 1) Install the full stack from PyPI.
#    fd-open-data-protocol is pulled in transitively; fd-polygon and
#    fd-cn-report auto-register via entry-points. Drop "[data]" for a lighter
#    install (MCP server + CLI only, without the akshare/yfinance/playwright SDKs).
pip install "fd-open-data-mcp[data]" fd-polygon fd-cn-report

# 2) Initialize the ontology DB and wire every layer: catalogs -> concepts ->
#    column bindings -> per-source entity ids -> refresh schedules -> manifests.
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

Live data fetches need source keys in the environment (never committed):
`POLYGON_API_KEY`, `EDGAR_IDENTITY`, and the `LLM_*` / `ES_*` set for
`fd-cn-report`. See each package's Configuration section.

## Architecture

```
CONSUMED (read-only)                  ADDED by fd-open-data-mcp
 fd-akshare / yfinance / edgar /        concept_bindings      (column -> concept)
 wbgapi / cn-report / cn-gov /           entity_source_identifiers (per-source id)
 datacommons / polygon registries        source_rankings       (quality × access × freshness)
 fd-entities-indicators                 semantic_observations (read-through cache)
   indicator_defs (concepts)             fetch_log / schedules / executions / policies
   countries/cities/symbols/sw_industries   entities / relationships (graph)
        │
   TRANSFORMERS: import_catalog, consume_concepts, propose_bindings,
                 seed_entity_identifiers, generate_refresh_schedules, ingest_entities
        │
   RUNTIME: read() -> cache hit? : dispatch (ranked, failover) -> cache -> log
   SEARCH : semantic_search (concepts) + graph_search (entity relationships) + ai_search
```

Eight capability areas (see `openspec/changes/add-fd-open-data-mcp/specs/`):
`open-data-catalog`, `semantic-layer`, `entity-identity`, `source-ranking`,
`concept-fetch`, `scheduled-refresh`, `entity-graph`, `vector-search`.

## Install

```bash
cd fd-open-data-mcp
uv sync                  # base install

# For full data source support (akshare, yfinance, edgar, world bank, etc.)
uv sync --extra data
```

The DB path defaults to `fd_open_data_mcp/metadata/daas.db`; override with
`FD_OPEN_DATA_MCP_DATABASE_URL`. `FINDDATA_ROOT` (default: the parent
`finddata/` dir) locates the `fd-*` providers.

> **SEC EDGAR** requires `EDGAR_IDENTITY="your_email@example.com"` in the
> environment before use (the SEC mandates a User-Agent for anonymous access).

## Quickstart

```bash
# 1. create the ontology tables
fd-open-data-mcp migrate

# 2. import the catalogs (akshare, yfinance, cn-gov, cn-report, edgar, ...)
fd-open-data-mcp import-catalog
# or one provider:  fd-open-data-mcp import-catalog akshare

# 3. consume indicator_defs as concepts + propose column->concept bindings
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

The MCP tool surface is organized into eight capability areas (use the tool
list in your MCP client for the authoritative set):

| Area | Representative tools |
|------|----------------------|
| Catalog / import | `import_catalog`, `register_datasource`, `register_discovered`, `consume_concepts`, `enumerate_wbgapi_indicators`, `ingest_entities_from_dump` |
| Entity identity | `seed_entity_identifiers`, `resolve_entity`, `add_entity`, `add_entity_identifier`, `update_entity`, `get_entity`, `list_entities` |
| Semantic layer | `list_concepts`, `update_concept`, `re_embed_concept`, `propose_bindings`, `list_bindings`, `review_bindings`, `confirm_binding`, `update_binding`, `rank_sources` |
| Entity graph | `add_relationship`, `list_relationships`, `graph_search` |
| Vector search | `semantic_search`, `semantic_search_entities`, `semantic_search_unified`, `ai_search` |
| Fetch | `read`, `fetch`, `plan_crawl` |
| Scheduled refresh | `generate_refresh_schedules`, `list_schedules`, `run_schedule` |
| Crawl policies | `policy_create`, `policy_list`, `policy_get`, `policy_update`, `policy_estimate`, `policy_trigger_now`, `policy_runs`, `policy_enable`, `policy_disable`, `policy_delete` |
| cn-report rules | `list_cnreport_rules` |

`ai_search` is the end-to-end entry point: semantic search → graph traversal →
value query, in one call.

## Data sources

Sources are wired in `fd_open_data_mcp/fetch/runner.py::run_upstream()`, a
hardcoded source→runner chain. The table below reflects the **actual** state
of each adapter, not aspirational status.

### Production (network-backed)

| Source | Adapter | Coverage |
|--------|---------|----------|
| `akshare` | `adapters/akshare.py` | A-share stocks, funds, financial statements (eastmoney/tencent/sina failover) |
| `yfinance` | `adapters/yfinance.py` | Yahoo Finance global equities |
| `edgar` | `adapters/edgar.py` | SEC EDGAR filings (needs `EDGAR_IDENTITY`) |
| `edinet` | `adapters/edinet.py` | Japan EDINET disclosures |
| `dartlab` | `adapters/dartlab.py` | Korea DART corporate filings |
| `wbgapi` | `adapters/wbgapi.py` | World Bank WDI |
| `nbs-gdp` | `adapters/nbs_gdp.py` | China NBS GDP macro series |
| `cisa-industry` | `adapters/cisa_industry.py` | China Iron & Steel Association |
| `ckan` | `adapters/ckan.py` | CKAN catalog ingest |
| `cnstats` | `adapters/cnstats.py` | Chinese NBS statistics |
| `cn-report` | `adapters/cnreport.py` | Chinese financial-report extraction (delegates to `fd-cn-report`) |
| `polygon` | external `fd-polygon` pkg | US equity OHLCV + company reference (needs `POLYGON_API_KEY`) |
| `datacommons` | external `fd-datacommons` pkg | Google Data Commons (needs `DC_API_KEY`) |

External datasource packages (`polygon`, `datacommons`) are lazy-imported at
fetch time, so `fd-open-data-mcp` does not depend on their SDKs unless a fetch
is actually made.

### Stub / placeholder

These adapters exist and are dispatchable but return **placeholder data** —
they are scaffolds for future scraping work, not usable data sources:

`amac-fund`, `shfe-metal-futures`, `agriculture` (DCE), `cme-agricultural-futures`,
`chemicals`, `electronics`, `nonferrous`, `flowers-kifc`, `fin_platforms`,
`sac-securities`.

> **Note:** the `fd-open-data-mcp list-sources` CLI marks every adapter
> "✅ Full support". That label is **not** an integration guarantee — it only
> checks that an adapter file exists. Treat the stub list above as
> authoritative.

### Read-only registries

| Source | Status |
|--------|--------|
| `cn-gov` | Read-only registry (manifest-based; 11 CN ministries) |
| `world` | Read-only catalog (CKAN + Chinese NBS) |

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
- `FD_PROXY_FORWARDER` — unset for local dev (the injection shim returns a
  direct sentinel → direct egress; the standalone `fd-proxy-service` forwarder
  owns proxy selection in cluster crawls). The legacy `FD_PROXY_POOL`/
  `FD_EGRESS_MODE` vars are no longer read.

**Panel pages** (all server-rendered, no build step, all under the token gate):

| Page | What it answers |
|---|---|
| `/panel` | Observability home: fleet health, running runs with live attempted/new counters (htmx polling, 15 s), recent finished runs with yield classification, next-up schedule, stale/suspended-scheduler banner |
| `/panel/policies` | Target management: list, enable/disable, editor with fetch estimate |
| `/panel/runs`, `/panel/runs/{id}` | Run list + drill-down: compiled plan, yield vs plan cells, job ref, window-approximated fetch outcomes |
| `/panel/data` | Data coverage: per-concept rows / latest date / sources / last fetch over `semantic_observations`, plus a per-store census (local master exact + each shard's catalog estimate, chunk count, data time-range end) with a refresh action |

The same reads exist as MCP tools: `crawl_status` (snapshot incl. `next_runs`)
and `data_stats` (per-concept coverage + stores census) — panel and tools share
one query layer (`visibility/snapshot.py`, `visibility/coverage.py`,
`visibility/census.py`). The shard census is collected only by explicit
refresh (`POST /panel/data/census/refresh` or `fd-open-data-mcp census`) using
catalog-only probes over `dblink` (run `CREATE EXTENSION dblink` on the master
once); it never scans shard fact tables (runbook OOM constraint).

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

## CLI

```bash
fd-open-data-mcp migrate                 # create ontology tables
fd-open-data-mcp import-catalog [src]    # import fd-* catalogs
fd-open-data-mcp consume-concepts        # indicator_defs -> concepts
fd-open-data-mcp propose-bindings        # column -> concept bindings
fd-open-data-mcp seed-entities           # per-source entity identifiers
fd-open-data-mcp generate-schedules     # per-concept refresh schedules
fd-open-data-mcp plan-crawl ...         # compile a CrawlPlan
fd-open-data-mcp read --concept-id N --entity-type stock --entity-id 1 --date YYYY-MM-DD
fd-open-data-mcp rank-sources --concept-id N
fd-open-data-mcp register-datasource <path>
fd-open-data-mcp register-discovered    # auto-discover entry-point manifests
fd-open-data-mcp list-sources           # adapter inventory (see caveat above)
fd-open-data-mcp serve                  # MCP server (stdio)
fd-open-data-mcp panel                  # crawl control panel
```

Proxy-pool ops (cluster): `seed-proxy-health`, `probe-cycle`, `proxy-health`.

## Tests

```bash
uv run --with pytest pytest -q
```

## LLM configuration (for PDF report extraction)

`fd-cn-report` uses an LLM to extract financial indicators from annual-report
PDFs. It runs in the same environment as `fd-open-data-mcp` and is configured
via the `LLM_*` env vars in `.env` / `.env.local`:

```bash
LLM_BASE_URL=https://api.plan/v1          # Ark endpoint
LLM_API_KEY=<your-ark-key>                # Ark API key
LLM_MODEL=deepseek-v4-flash              # default model
```

The default provider is **DeepSeek on Ark**. Any OpenAI-compatible
`LLM_BASE_URL` (OpenAI, Azure OpenAI, OpenRouter, local Ollama) also works —
point `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` at it. `LLM_API_KEY` takes
priority over `OPENAI_API_KEY` if both are set.

## Design notes / limitations

- **Propose-and-confirm**: column->concept bindings carry `confidence` +
  `provenance`; below-threshold bindings are withheld from dispatch (review
  queue). A real fetch promotes a binding to `sample-confirmed`.
- **Ranking** is per `(source × concept)`, self-tuning from `fetch_log`
  (bounded so one failure can't remove a source).
- **Conflict policy**: one cached value per `(concept, entity, date)` with
  `source_used` attached; values are never merged across sources.
- **Vector search** uses JSONB + numpy (pgvector unavailable on the target
  Postgres); concept + entity embeddings power `semantic_search*` and
  `ai_search`.
- **Real-source failover**: functions declare `real_sources` (e.g.
  `stock_zh_a_hist` → `[eastmoney, tencent, sina]`); when `eastmoney` is
  banned, the dispatcher fails over to `tencent`/`sina`. Circuit-breaker keys
  are per real-source, not per library.
- `_build_params` / `_extract_value` in the fetch runner are best-effort; a
  production runner refines per-function date-format / payload-shape quirks.

See `openspec/changes/add-fd-open-data-mcp/` for the full spec and
`openspec/changes/add-source-proxy-health/` for the proxy/circuit-breaker
design.

## Contributing

To add a new datasource:

1. Author a manifest per `fd-open-data-protocol` (YAML/JSON or a `CATALOG` dict).
2. Expose it via the `fd_open_data_mcp.datasources` entry-point in your
   package's `pyproject.toml`, or `fd-open-data-mcp register-datasource <path>`.
3. If the fetch logic can't be expressed as a built-in runner, ship a
   `run_<source>(command, params)` in an adapter (or an external package) and
   branch on it in `run_upstream()`.
4. `fd-open-data-mcp register-discovered` then ingests it; `propose-bindings`
   binds its columns to concepts.

## License

MIT
