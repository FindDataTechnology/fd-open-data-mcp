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
uv sync --extra data     # + akshare / yfinance / world_bank_data (for real fetches)
```

The DB path defaults to `fd_open_data_mcp/metadata/daas.db`; override with
`FD_OPEN_DATA_MCP_DATABASE_URL`. `FINDDATA_ROOT` (default: the parent
`finddata/` dir) locates the `fd-*` providers. `EDGAR_IDENTITY` (an email)
is required by the SEC before any edgar fetch - the runner refuses to call
anonymously if it is unset.

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
