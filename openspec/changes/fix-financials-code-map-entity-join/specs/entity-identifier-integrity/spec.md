# entity-identifier-integrity Specification

## ADDED Requirements

### Requirement: A code→entity map SHALL verify the entity's own type

Any map that resolves a source code to an entity for writing observations SHALL
join the `entities` row and require the entity's own `entity_type`, rather than
trusting the `entity_type` recorded on the identifier row. Colliding codes SHALL
be reported, not silently resolved last-wins.

#### Scenario: A mislabelled identifier cannot redirect financials

- **WHEN** a stock-typed identifier points at an entity whose own type is `industry` (or any non-stock type)
- **THEN** that code is absent from the code→entity map, so the ingester skips it instead of writing financials onto the industry

#### Scenario: Two entities claim one code

- **WHEN** two stock entities share an identifier for the same code
- **THEN** the map keeps the first and logs the conflict, so a rerun is deterministic
