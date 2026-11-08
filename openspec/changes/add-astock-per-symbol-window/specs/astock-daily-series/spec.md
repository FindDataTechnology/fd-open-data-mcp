# astock-daily-series Specification

## ADDED Requirements

### Requirement: A symbol's window SHALL start at its own last stored date

A daily run SHALL compute each symbol's fetch start as the later of the run's
global start and that symbol's own last stored date + 1, capped to the most
recent `--max-catchup-days` before the run's end date. A symbol that has fallen
behind SHALL therefore be caught up by the standing run, and a current symbol
SHALL keep exactly the global window. An explicit `--start` SHALL override the
per-symbol computation for deliberate backfills.

#### Scenario: A lagging symbol is caught up

- **WHEN** a symbol's last stored date is older than the global start and the gap is within the cap
- **THEN** that symbol is fetched from its own last stored date + 1 and the run reports it as caught up

#### Scenario: The cap bounds the work

- **WHEN** a symbol's gap exceeds `--max-catchup-days`
- **THEN** the fetch starts at the cap boundary and the run reports the symbol as capped

#### Scenario: A current symbol is unaffected

- **WHEN** a symbol's last stored date is on or after the global start
- **THEN** its fetch uses the global window, exactly as before

#### Scenario: An explicit window still wins

- **WHEN** `--start` is passed
- **THEN** every symbol uses that start regardless of its own last stored date
