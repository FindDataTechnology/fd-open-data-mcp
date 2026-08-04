# Protocol Migration Generator for Datasource Catalogs

## Why

External datasource packages (fd-akshare, fd-yfinance, fd-edgar, fd-wbgapi) maintain their own registries but don't expose protocol-compliant CATALOG declarations. Manual maintenance of catalog.py files is prohibitively expensive due to 600+ functions in akshare alone and frequent upstream changes.

## What Changes

Add an incremental migration tool that:
1. **Generates minimal catalogs** from existing source-of-truth files (registry.db, seed.py)
2. **Leaves enrichment points** for manual concept/entity declarations
3. **Runs on-demand or periodically** to keep catalogs in sync
4. **Requires no package installation** - reads files directly

### Breaking Changes

**None** - this adds new infrastructure without modifying existing behavior.

## Capabilities

### New Capabilities

- **catalog-generator**: CLI tool that introspects datasource registries and generates protocol-compliant CATALOG declarations, with support for incremental enrichment

### Modified Capabilities

_None_

## Impact

### Affected Code

- `fd-open-data-mcp/catalog/generator.py` - main generation logic
- `fd-open-data-mcp/catalog/generators/akshare.py` - akshare-specific parser
- `fd-open-data-mcp/catalog/generators/yfinance.py` - yfinance-specific parser
- `fd-open-data-mcp/catalog/generators/edgar.py` - edgar-specific parser
- `fd-open-data-mcp/catalog/generators/wbgapi.py` - wbgapi-specific parser
- `fd-open-data-mcp/catalog/enrichments/` - manual concept hints per source
- `fd-akshare/`, `fd-yfinance/`, `fd-edgar/`, `fd-wbgapi/` - each gets catalog.py output

### Architecture Diagram

```
┌─────────────────────────────────────────────────────────────┐
│  Incremental Migration Flow                                │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  Sources                                                    │
│  ├─ fd-akshare/metadata/registry.db                        │
│  ├─ fd-yfinance/core/seed.py                               │
│  ├─ fd-edgar/seeds/edgar.py                                │
│  └─ fd-wbgapi/seeds/wbgapi.py                              │
│                                                             │
│       ↓ read                                                │
│                                                             │
│  ┌────────────────────┐                                    │
│  │ Generator CLI      │                                    │
│  │ python -m gen \    │                                    │
│  │   --source akshare │                                    │
│  │   --output ...     │                                    │
│  └────────┬───────────┘                                    │
│           ↓                                               │
│                                                             │
│  Output: fd-akshare/fd_akshare/catalog.py                  │
│  ├─ Auto-generated: functions list                         │
│  ├─ Auto-generated: basic metadata                         │
│  └─ Empty: concepts/entities (ready for enrichment)        │
│                                                             │
│  Manual Enrichment Layer                                    │
│  ├─ fd-akshare/fd_akshare/enrichments/concepts.py         │
│  └─ Adds semantic mappings for high-value functions        │
│                                                             │
│  Result: Protocol-compliant CATALOG                        │
│  └─ discover_datasources() finds it via entry-point        │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### Risks

- **Catalog drift**: If generator isn't run when upstream changes, catalog becomes stale. Mitigation: weekly cron job + developer reminder on commit.
- **Enrichment neglect**: Concept hints stay empty if developers don't add them. Mitigation: prioritize high-value functions first.
- **Per-source complexity**: Need separate parsers for each source format. Mitigation: start with one source, iterate.
