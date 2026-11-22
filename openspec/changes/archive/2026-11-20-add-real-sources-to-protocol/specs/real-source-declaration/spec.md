## ADDED Requirements

### Requirement: RealSourceSpec model
The `fd-open-data-protocol` package SHALL define a `RealSourceSpec` Pydantic model with fields: `name` (str, required), `priority` (int, default 0), `endpoint` (Optional[str], default None). The `name` field SHALL identify the real data source (e.g., "eastmoney", "tencent", "sina", "yahoo_finance"). The `priority` field SHALL indicate failover order (0 = primary, 1+ = failover). The `endpoint` field SHALL optionally specify a specific method or URL.

#### Scenario: Minimal RealSourceSpec
- **WHEN** a `RealSourceSpec` is created with `name="eastmoney"`
- **THEN** `priority` SHALL default to 0
- **AND** `endpoint` SHALL default to None

#### Scenario: RealSourceSpec with failover priority
- **WHEN** a `RealSourceSpec` is created with `name="tencent", priority=1`
- **THEN** `priority` SHALL be 1
- **AND** the spec SHALL be valid

#### Scenario: RealSourceSpec with endpoint
- **WHEN** a `RealSourceSpec` is created with `name="eastmoney", endpoint="stock_zh_a_hist"`
- **THEN** `endpoint` SHALL be "stock_zh_a_hist"
- **AND** the spec SHALL be valid

### Requirement: FunctionSpec.real_sources field
The `FunctionSpec` model SHALL have an optional `real_sources` field of type `list[RealSourceSpec]`. When present, the field SHALL declare which real data sources the function calls, in priority order. When absent, the system SHALL fall back to the library-level `source` (backward compatibility).

#### Scenario: FunctionSpec with real_sources
- **WHEN** a `FunctionSpec` is created with `real_sources=[RealSourceSpec(name="eastmoney", priority=0), RealSourceSpec(name="tencent", priority=1)]`
- **THEN** the function SHALL declare two real sources
- **AND** eastmoney SHALL be the primary source (priority 0)
- **AND** tencent SHALL be the failover source (priority 1)

#### Scenario: FunctionSpec without real_sources (backward compatibility)
- **WHEN** a `FunctionSpec` is created without `real_sources`
- **THEN** `real_sources` SHALL be None
- **AND** the system SHALL fall back to the library-level `source`

#### Scenario: FunctionSpec with empty real_sources
- **WHEN** a `FunctionSpec` is created with `real_sources=[]`
- **THEN** the function SHALL declare no real sources
- **AND** the system SHALL fall back to the library-level `source`

### Requirement: Manifest validation with real_sources
The `DatasourceManifest` validator SHALL accept functions with `real_sources` field. The validator SHALL NOT require `real_sources` (backward compatibility). The validator SHALL validate each `RealSourceSpec` in the list.

#### Scenario: Valid manifest with real_sources
- **WHEN** a manifest is loaded with a function declaring `real_sources`
- **THEN** the manifest SHALL validate successfully
- **AND** each `RealSourceSpec` SHALL be validated

#### Scenario: Valid manifest without real_sources
- **WHEN** a manifest is loaded with a function not declaring `real_sources`
- **THEN** the manifest SHALL validate successfully
- **AND** `real_sources` SHALL be None

### Requirement: YAML manifest example with real_sources
The `fd-open-data-protocol` package SHALL ship an example YAML manifest demonstrating `real_sources` usage. The example SHALL show a function with multiple real sources and priorities.

#### Scenario: Example manifest loads and validates
- **WHEN** the example manifest is loaded via `load_catalog()`
- **THEN** it SHALL return a valid `DatasourceManifest`
- **AND** at least one function SHALL have `real_sources` declared
