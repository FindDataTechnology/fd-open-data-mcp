## ADDED Requirements

### Requirement: Stock entity source identifier derivation
For every `entity_type='stock'` entity, the system SHALL store per-source identifiers derived from the entity's 6-digit `code` (the A-share ticker): the `akshare` identifier SHALL equal the bare 6-digit code; the `yfinance` identifier SHALL equal the code suffixed with `.SS` for Shanghai codes (prefix `60`) or `.SZ` for Shenzhen codes (prefix `00`/`30`); the `cn-report` identifier SHALL equal the 6-digit code.

#### Scenario: Bank of China resolves to its ticker
- **WHEN** `resolve_entity(entity_type='stock', entity_id=3112, source='akshare')` is called
- **THEN** the system SHALL return `"601988"`
- **AND** `resolve_entity(..., source='yfinance')` SHALL return `"601988.SS"`
- **AND** `resolve_entity(..., source='cn-report')` SHALL return `"601988"`

#### Scenario: Shenzhen stock resolves with SZ suffix
- **WHEN** `resolve_entity(entity_type='stock', entity_id=3117, source='yfinance')` is called (平安银行, code `000001`)
- **THEN** the system SHALL return `"000001.SZ"`
- **AND** `resolve_entity(..., source='akshare')` SHALL return `"000001"`

### Requirement: Source identifier validation and repair
The system SHALL provide a CLI subcommand that reports stock entities whose `akshare` or `yfinance` identifier diverges from the derived-canonical value, and SHALL repair them idempotently in a single batch transaction when invoked with `--apply`.

#### Scenario: Dry-run drift report
- **WHEN** the repair subcommand is run without `--apply`
- **THEN** the system SHALL list every stock entity whose stored `akshare` identifier != its 6-digit `code`
- **AND** the list SHALL include entity id, code, stored identifier, and the canonical identifier
- **AND** no rows SHALL be modified

#### Scenario: Idempotent repair
- **WHEN** the repair subcommand is run with `--apply`
- **THEN** the system SHALL upsert the canonical `akshare` and `yfinance` identifiers for all drift rows
- **AND** running the subcommand with `--apply` a second time SHALL report zero drift rows
- **AND** `cn-report` identifiers SHALL NOT be modified

### Requirement: Seeding derives from entity code
The `seed_entity_identifiers` function (and any registration path that creates stock entity identifiers) SHALL derive `akshare` and `yfinance` identifiers from `entity.code` directly, not from a secondary lookup table or industry code.

#### Scenario: New stock entity seeded correctly
- **WHEN** a new `stock` entity with code `601988` is registered
- **THEN** the seeding SHALL write `akshare=601988`, `yfinance=601988.SS`, `cn-report=601988`
- **AND** SHALL NOT write any identifier that is not derivable from the 6-digit code
