# entity-identifier-integrity Specification

## ADDED Requirements

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
