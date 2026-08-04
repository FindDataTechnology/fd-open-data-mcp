# Catalog Generator Specification

## Overview

The catalog generator is a CLI tool that generates `catalog.py` files for fd-* datasource packages, converting their existing metadata formats (registry.db, seed.py) into protocol-compliant DatasourceManifest files.

## Requirements

### 1. Source-Specific Parsers

#### 1.1 Akshare Parser

**REQUIREMENT:** The akshare parser SHALL read from `fd-akshare/metadata/registry.db` SQLite database.

The parser SHALL:
- Connect to the SQLite database
- Read all rows from `functions` table
- Read all rows from `function_columns` table
- Join functions with their columns
- Output FunctionSpec objects conforming to DatasourceManifest schema

**Scenario:** Parse akshare registry.db

```
WHEN run_catalog_generator --source akshare --input /path/to/fd-akshare/metadata/registry.db
THEN generate catalog.py with all functions from registry.db
AND map function_columns to column definitions in each function
```

#### 1.2 Yfinance Parser

**REQUIREMENT:** The yfinance parser SHALL read from `fd-yfinance/core/seed.py` Python file.

The parser SHALL:
- Parse the seed.py file without executing it (ast.parse or exec in restricted namespace)
- Extract the REGISTRY dict variable
- Iterate through REGISTRY entries
- Output FunctionSpec objects

**Scenario:** Parse yfinance seed.py

```
WHEN run_catalog_generator --source yfinance --input /path/to/fd-yfinance/core/seed.py
THEN extract REGISTRY dict and convert to catalog format
AND handle nested structures like "columns" field if present
```

#### 1.3 Edgar Parser

**REQUIREMENT:** The edgar parser SHALL read from `fd-edgar/seeds/edgar.py` Python file.

The parser SHALL work like yfinance parser but handle the specific structure of fd-edgar's REGISTRY.

**Scenario:** Parse edgar seed.py

```
WHEN run_catalog_generator --source edgar --input /path/to/fd-edgar/seeds/edgar.py
THEN extract and convert REGISTRY to catalog format
```

#### 1.4 WBGAPI Parser

**REQUIREMENT:** The wbgapi parser SHALL read from `fd-wbgapi/seeds/wbgapi.py` Python file.

**Scenario:** Parse wbgapi seed.py

```
WHEN run_catalog_generator --source wbgapi --input /path/to/fd-wbgapi/seeds/wbgapi.py
THEN extract and convert REGISTRY to catalog format
```

### 2. Output Format

**REQUIREMENT:** The generated catalog.py MUST conform to the DatasourceManifest schema.

The output MUST include:
- version: "1"
- name: source package name
- label: human-readable label
- functions: list of FunctionSpec objects
- concepts: empty list (to be enriched manually later)
- entities: basic entity coverage declarations
- fetch: module reference for runtime dispatch

**Scenario:** Validate generated catalog

```
WHEN load_catalog("generated_catalog.py") is called
THEN validation passes against DatasourceManifest schema
AND no Pydantic validation errors are raised
```

### 3. Manual Enrichment Support

**REQUIREMENT:** The generator MUST support manual enrichment files.

A separate enrichment file (e.g., `enrichments/concepts.py`) SHOULD contain:
- Concept hints mapping columns to semantic concepts
- Entity coverage details
- These are loaded separately and merged with the auto-generated catalog

**Scenario:** Apply enrichments

```
WHEN run_catalog_generator --source akshare --enrichments enrichments/akshare_concepts.py
THEN merge enrichment data with generated catalog
AND produce final catalog.py with both auto-generated and manual fields
```

### 4. CI Integration

**REQUIREMENT:** The generator MUST be callable from CI/CD pipelines.

The tool MUST:
- Exit with code 0 on success, non-zero on failure
- Accept input/output paths as command-line arguments
- Produce deterministic output (same input → same output)

**Scenario:** Weekly CI regeneration

```
WHEN cron job runs ./scripts/regenerate-catalogs.sh daily
THEN catalog.py files are regenerated from source metadata
AND git commit/push only if changes detected
AND PR created with diff showing what changed
```
