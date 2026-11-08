# entity-identifier-integrity Specification

## Purpose
What an identifier row may claim about its entity: an identifier's
`entity_type` must equal the type of the entity it points at, so a code→entity
resolution keyed on the identifier table can never return an entity of another
class (a fund code resolving to a fund manager was the live case).

## Requirements

### Requirement: An identifier's type SHALL match its entity's type

Every row in `entity_source_identifiers` SHALL carry the same `entity_type` as
its `entities` row, so a code→entity resolution keyed on the identifier table
can never return an entity of another class. Identifiers that cannot belong to
their entity (a fund code on a person) SHALL be removed rather than re-typed.

#### Scenario: A fund code no longer resolves to a person

- **WHEN** the correction has run
- **THEN** no `entity_type='stock'` identifier points at an entity whose type is `person`, and the ETF codes that used to do so resolve to nothing rather than to a manager

#### Scenario: A fund's identifiers are typed as the fund

- **WHEN** a `stock`-typed identifier belongs to a `fund` entity
- **THEN** the identifier is re-typed to `fund` and the code still resolves to that fund

#### Scenario: Deferred classes stay visible

- **WHEN** the mismatch matrix is re-run after the correction
- **THEN** only the deliberately deferred classes (organization / industry / index / exchange, country-on-city) remain, each listed as debt with its count

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
