# observation-reading Specification

## ADDED Requirements

### Requirement: A series read SHALL resolve the entity's own class

Any read that returns observations for an (entity type, entity id) pair SHALL
verify the pair against the `entities` row: the class a row declares SHALL be the
class of the entity it hangs off, and for a security concept (`stock`) the entity
itself SHALL be a stock entity. A mismatch SHALL be reported as an explicit error
naming the entity's real class, never as an empty series that pretends the data
is merely missing.

The check SHALL NOT be expressed as "the entity's class equals the concept's
class": one concept legitimately spans several levels (the star-rated survey
stores city, region and country rows under a region-typed concept), and that
equality would hide those series.

#### Scenario: A stock concept asked about an industry entity

- **WHEN** `read_series` is called with a stock price/financials concept and the id of an entity whose own class is `industry`
- **THEN** the read fails with an error naming the entity as `industry`, not `stock`, and says the rows of that class on such an entity are mis-attached data rather than a coverage gap

#### Scenario: A fund manager's entity id

- **WHEN** a stock concept is read with the id of a `person` entity (865 fund managers carry 333,975 mis-attached OHLCV rows)
- **THEN** the read is refused the same way, so a manager's page cannot show a company's price

#### Scenario: A genuine stock still reads

- **WHEN** the entity id belongs to a stock entity
- **THEN** the series is returned unchanged, including its points, sources and caliber caveats

#### Scenario: A multi-level concept keeps its city rows

- **WHEN** the star-rated survey is read for a city entity (its 5,565 city rows hang off `city` entities under a region-typed concept)
- **THEN** the city-level series is returned, because the row's declared class matches the city entity it hangs off

#### Scenario: A dangling entity id is a data gap, not a tool error

- **WHEN** the entity id has no `entities` row at all
- **THEN** the read does not raise on that account; dangling ids are counted and reported separately
