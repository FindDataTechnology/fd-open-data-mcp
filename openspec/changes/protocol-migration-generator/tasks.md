## 1. Build core CLI generator

- [x] 1.1 Create basic CLI structure with argparse (--source, --input, --output flags)
- [x] 1.2 Implement SQLite reader for akshare registry.db (read functions table)
- [x] 1.3 Implement Python file parser using ast module (parse REGISTRY dict without executing)
- [x] 1.4 Define DatasourceManifest output schema in pydantic
- [x] 1.5 Test with sample data from fd-akshare/metadata/registry.db

## 2. Add source-specific parsers

- [x] 2.1 Akshare parser: read all rows from functions + function_columns tables
- [x] 2.2 Yfinance parser: parse seed.py file, extract REGISTRY dict
- [x] 2.3 Edgar parser: parse seeds/edgar.py, handle its specific REGISTRY format
- [x] 2.4 Wbgapi parser: parse seeds/wbgapi.py, handle its specific REGISTRY format
- [ ] 2.5 Document each parser's input requirements and limitations

## 3. Generate catalog output

- [x] 3.1 Implement catalog.py file writer (valid Python syntax)
- [x] 3.2 Generate basic fields: version, name, label, fetch spec
- [x] 3.3 Generate functions[] array with command, parameters, columns
- [x] 3.4 Leave concepts[] and entities[] empty or minimal
- [x] 3.5 Add comment header noting auto-generated nature
- [ ] 3.6 Validate output against DatasourceManifest schema

## 4. Add manual enrichment support

- [ ] 4.1 Create template enrichments/concepts.py file per source
- [ ] 4.2 Define standard concept hint format (column → concept mapping)
- [ ] 4.3 Allow loading enrichment files and merging into generated catalog
- [ ] 4.4 Document enrichment process for developers

## 5. Testing

- [ ] 5.1 Unit tests for SQLite reader (fd-akshare)
- [ ] 5.2 Unit tests for Python file parser (fd-yfinance, fd-edgar, fd-wbgapi)
- [ ] 5.3 Integration test: generate catalog for actual fd-akshare package
- [ ] 5.4 Verify load_catalog() can import generated catalog.py
- [ ] 5.5 Compare function counts between original metadata and generated catalog

## 6. Documentation

- [ ] 6.1 Write README.md explaining how to use the generator
- [ ] 6.2 Add usage examples for each datasource type
- [ ] 6.3 Create CONTRIBUTING.md guide for adding new source types
- [ ] 6.4 Update CLAUDE.md with migration workflow

## 7. CI/CD Integration

- [ ] 7.1 Create weekly GitHub Actions workflow for regeneration
- [ ] 7.2 Implement git diff detection (commit only if changes)
- [ ] 7.3 Create PR automation for catalog updates
- [ ] 7.4 Add cron job scheduling documentation

## 8. Final cleanup

- [ ] 8.1 Review and refine all generators
- [ ] 8.2 Add error handling and informative messages
- [ ] 8.3 Optimize performance for large datasets (600+ functions)
- [ ] 8.4 Archive this change after validation
