## Context

The protocol-compliant CATALOG declarations exist for fd-world, fd-cn-gov, and fd-cn-report, but not for fd-akshare, fd-yfinance, fd-edgar, and fd-wbgapi. These external packages maintain their own metadata structures (registry.db, seed.py) that could be used to generate protocol-compatible catalogs.

Currently there's no tool to convert between these internal metadata formats and the DatasourceManifest schema. Manual catalog creation is error-prone and hard to maintain.

## Goals / Non-Goals

**Goals:**
1. Create a simple CLI tool that reads source metadata and outputs catalog.py files
2. Support multiple source formats (SQLite registry.db, Python REGISTRY dict files)
3. Generate valid DatasourceManifest with basic fields (functions, parameters, columns)
4. Leave concept hints empty or minimal for manual enrichment later
5. Make it easy to run manually on demand

**Non-Goals:**
1. Automatic introspection of running code (too complex, rate-limited)
2. Auto-generation of concept hints or semantic metadata
3. CI/CD integration (that's future work)
4. Coverage of all edge cases in upstream APIs

## Decisions

### Decision 1: Standalone CLI Tool

Choose: `fd-catalog-generator` as a separate pip-installable tool

Rationale:
- Can be installed independently without installing the source packages
- Reusable across different datasources
- Clear separation of concerns
- Easy to update generator logic without touching datasource packages

Alternatives considered:
| Option | Pros | Cons |
|--------|------|------|
| In fd-open-data-mcp | Centralized | Tightly coupled, needs package deps |
| In each datasource package | Self-contained | Duplication, harder to coordinate |
| Standalone CLI (chosen) | Independent, reusable | Extra package to manage |

### Decision 2: File-Based Input

Choose: Read from known file paths (registry.db, seed.py) rather than package introspection

Rationale:
- Works even if packages aren't installed
- Direct access to source of truth
- No runtime dependencies
- Deterministic output

Example usage:
```bash
python -m fd_catalog_generator \
  --source akshare \
  --input /path/to/fd-akshare/fd_akshare/metadata/registry.db \
  --output /path/to/fd-akshare/fd_akshare/catalog.py
```

### Decision 3: Partial Output First

Choose: Generate only required fields (name, label, functions with basic params), leave concepts/entities empty

Rationale:
- Faster initial implementation
- Can validate structure quickly
- Manual enrichment layer can add semantic metadata later
- Lower bar for "good enough" first version

### Decision 4: Per-Source Parsers

Choose: Dedicated parser module per source format

Structure:
```
fd-catalog-generator/
├─ generators/
│  ├─ akshare_db.py       # Reads registry.db SQLite
│  ├─ yfinance_seed.py    # Parses Python REGISTRY dict
│  └─ edgar_seeds.py      # Parses Python REGISTRY dict
├─ enrichments/
│  ├─ akshare_concepts.py # Manual concept hints
│  └─ yfinance_concepts.py
└─ main.py                 # CLI entrypoint
```

Rationale:
- Clear separation between "auto" and "manual" parts
- Easy to extend with new sources
- Enrichments can be reviewed/committed separately

## Risks / Trade-offs

| Risk | Impact | Mitigation |
|------|--------|------------|
| Source format changes | Generator breaks | Version generators with source packages |
| Missing fields | Catalog incomplete | Allow partial catalogs, fill gaps manually |
| Rate limits | Slow generation | Use cached local files, not live introspection |
| Over-complexity | Hard to maintain | Keep generator simple, add features incrementally |

## Migration Plan

Phase 1: Core generator (1-2 weeks)
1. Build skeleton CLI with argparse
2. Implement SQLite reader (for fd-akshare)
3. Generate valid DatasourceManifest
4. Test against fd-akshare registry.db

Phase 2: Additional sources (2-3 weeks)
5. Add Python file parser (for yfinance, edgar)
6. Test against each source's metadata
7. Generate catalogs for all 4 sources

Phase 3: Enrichment layer (ongoing)
8. Create enrichment template files
9. Manually add concept hints for high-value functions
10. Commit enriched catalogs back to source repos

## Open Questions

1. Should we include parameter type detection? (currently defaults to "str")
2. How do we handle functions with variable column outputs? (leave empty initially)
3. What's the cadence for regenerating catalogs? (manual on-demand, weekly cron?)
4. Do we need versioning for generated catalogs? (no, git commits serve as history)
