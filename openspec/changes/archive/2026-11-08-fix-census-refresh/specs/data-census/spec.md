# data-census Specification

## ADDED Requirements

### Requirement: Census size counters SHALL hold values beyond 32-bit range

The `data_census` row for a store SHALL be able to record `approx_rows` and
`total_size_bytes` for any store the crawler fleet can produce, including stores
larger than 2,147,483,647 bytes (the PostgreSQL `integer` ceiling). A refresh
whose computed size exceeds the 32-bit range SHALL persist successfully instead
of aborting the write.

#### Scenario: Store grows past the 32-bit byte ceiling

- **WHEN** the census refresh computes `total_size_bytes` greater than 2,147,483,647 for the local store
- **THEN** the `data_census` row is updated with that value and `sampled_at` advances to the refresh time

#### Scenario: Row counts stay 64-bit safe

- **WHEN** `approx_rows` for a shard store exceeds 2,147,483,647
- **THEN** the value is stored exactly, without truncation or a range error

#### Scenario: Existing rows are preserved by the widening

- **WHEN** the widening migration is applied to a database that already has census rows
- **THEN** every pre-existing `approx_rows` / `total_size_bytes` value is unchanged and readable

### Requirement: One failing store SHALL NOT discard the census

Each leg of a census refresh (the local store's count and size, the shard-server
listing, and every individual shard probe) SHALL be isolated so that a failure
in one leg records an error on that store's row and leaves the other stores'
results committable. A failed statement SHALL NOT abort the surrounding
transaction in a way that discards the whole census.

#### Scenario: A dead shard probe still commits the other stores

- **WHEN** one shard's `dblink` probe raises during a refresh
- **THEN** that shard's row carries the error text, the local store and every other shard are written with fresh values, and `sampled_at` advances

#### Scenario: The local leg failing does not lose the shards

- **WHEN** the local count/size query fails
- **THEN** the local row records the error and the shard rows still commit with their own results
