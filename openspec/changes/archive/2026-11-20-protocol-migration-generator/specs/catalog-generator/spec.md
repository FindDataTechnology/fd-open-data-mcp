# catalog-generator Specification

## ADDED Requirements

### Requirement: Akshare parser

The akshare parser SHALL read from `fd-akshare/metadata/registry.db` SQLite database.

#### Scenario: Parse akshare registry.db

- **WHEN** run_catalog_generator --source akshare --input /path/to/fd-akshare/metadata/registry.db
- **THEN** generate catalog.py with all functions from registry.db
- **AND** map function_columns to column definitions in each function

### Requirement: Yfinance parser

The yfinance parser SHALL read from `fd-yfinance/core/seed.py` Python file.

#### Scenario: Parse yfinance seed.py

- **WHEN** run_catalog_generator --source yfinance --input /path/to/fd-yfinance/core/seed.py
- **THEN** extract REGISTRY dict and convert to catalog format
- **AND** handle nested structures like "columns" field if present

### Requirement: Edgar parser

The edgar parser SHALL read from `fd-edgar/seeds/edgar.py` Python file.

#### Scenario: Parse edgar seed.py

- **WHEN** run_catalog_generator --source edgar --input /path/to/fd-edgar/seeds/edgar.py
- **THEN** extract and convert REGISTRY to catalog format

### Requirement: Wbgapi parser

The wbgapi parser SHALL read from `fd-wbgapi/seeds/wbgapi.py` Python file.

#### Scenario: Parse wbgapi seed.py

- **WHEN** run_catalog_generator --source wbgapi --input /path/to/fd-wbgapi/seeds/wbgapi.py
- **THEN** extract and convert REGISTRY to catalog format

### Requirement: Generated catalog shape

The generated catalog.py MUST conform to the DatasourceManifest schema.

#### Scenario: Validate generated catalog

- **WHEN** load_catalog("generated_catalog.py") is called
- **THEN** validation passes against DatasourceManifest schema
- **AND** no Pydantic validation errors are raised

### Requirement: Manual enrichment support

The generator MUST support manual enrichment files that override or extend the
generated catalog entries.

#### Scenario: Apply enrichments

- **WHEN** run_catalog_generator --source akshare --enrichments enrichments/akshare_concepts.py
- **THEN** merge enrichment data with generated catalog
- **AND** produce final catalog.py with both auto-generated and manual fields

### Requirement: CI integration

The generator MUST be callable from CI/CD pipelines.

#### Scenario: Weekly CI regeneration

- **WHEN** cron job runs ./scripts/regenerate-catalogs.sh daily
- **THEN** catalog.py files are regenerated from source metadata
- **AND** git commit/push only if changes detected
- **AND** PR created with diff showing what changed
