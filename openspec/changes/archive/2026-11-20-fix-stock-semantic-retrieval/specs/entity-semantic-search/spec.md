## ADDED Requirements

### Requirement: Discovery excludes deprecated concepts
`semantic_search`, `semantic_search_entities`, `semantic_search_unified`, and `ai_search` SHALL exclude concepts whose `deprecated=true` from results, regardless of similarity score.

#### Scenario: Deprecated ghost concept not returned
- **WHEN** a user searches `"中国银行收盘价"` and `PRICE_CLOSE` (deprecated) has a high similarity score
- **THEN** the system SHALL NOT return `PRICE_CLOSE`
- **AND** the canonical `price.close` concept SHALL appear in results if its similarity qualifies

### Requirement: Binding-aware discovery ranking
Among non-deprecated concept results, the system SHALL rank concepts that have at least one row in `concept_bindings` ahead of concepts with zero bindings, when similarity scores are within a tolerance band.

#### Scenario: Bound concept ranks above unbound at similar similarity
- **WHEN** two non-deprecated concepts have similarity scores within 0.05 of each other
- **AND** one has ≥1 binding and the other has zero
- **THEN** the bound concept SHALL rank higher in the results

### Requirement: ai_search excludes unbound concepts by default
`ai_search` SHALL exclude concepts with zero bindings from its results by default, because the tool's intent is to return retrievable data. A caller MAY opt into unbound-concept results via an `include_unbound` parameter.

#### Scenario: ai_search returns only retrievable concepts
- **WHEN** `ai_search(query="中国银行股价", include_values=true)` is called without `include_unbound`
- **THEN** every returned concept SHALL have at least one binding
- **AND** the concepts SHALL be usable as `read()`/`fetch()` targets

#### Scenario: Exploratory mode includes unbound
- **WHEN** `ai_search(query="...", include_unbound=true)` is called
- **THEN** unbound non-deprecated concepts MAY appear, ranked after bound concepts
