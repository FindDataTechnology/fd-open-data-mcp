## MODIFIED Requirements

### Requirement: Datasource manifest schema

The `fd-open-data-protocol` package SHALL define a manifest schema (`DatasourceManifest`) capturing a datasource's identity, functions, columns, concept hints, and fetch reference. Each `ColumnSpec` SHALL carry optional `frequency` and `datasource` fields (column-level, from `enrich-concept-identity`); `measure` and `entity_type` are concept-level, declared via `ConceptHint`. Each `FunctionSpec` SHALL carry an optional `real_sources` field (`list[RealSourceSpec]`) declaring which real data sources the function calls (multi-valued, with priority for failover). The schema SHALL be validated (pydantic).

#### Scenario: A minimal manifest validates

- **WHEN** a manifest with `name`, `label`, and one `function` (with `command` + one `column`) is loaded
- **THEN** it validates as a `DatasourceManifest`
- **AND** the column's `frequency`/`datasource` default to unset (nullable)
- **AND** the function's `real_sources` defaults to None

#### Scenario: A manifest with real_sources validates

- **WHEN** a manifest with a function declaring `real_sources: [{name: eastmoney, priority: 0}, {name: tencent, priority: 1}]` is loaded
- **THEN** it validates as a `DatasourceManifest`
- **AND** the function's `real_sources` is a list of two `RealSourceSpec` objects
- **AND** the first spec has `name="eastmoney"`, `priority=0`
- **AND** the second spec has `name="tencent"`, `priority=1`
