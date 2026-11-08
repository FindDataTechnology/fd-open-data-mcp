# astock-daily-series Specification

## ADDED Requirements

### Requirement: An explicitly named code SHALL be able to create its series

When an operator names a code explicitly (`--symbols` / `--symbols-file`) and
passes `--allow-new-series`, a code whose candidate entities hold no stored
`astock_daily` rows SHALL still resolve to exactly one entity, and its requested
window SHALL be ingested from the source's own adjusted series. Without the
flag, such a code SHALL be reported as having no stored rows and SHALL NOT be
ingested.

The universe path (`--universe`) SHALL remain restricted to codes that already
hold rows, so a routine run can never grow the family implicitly.

#### Scenario: A new listing gets its first series

- **WHEN** `--symbols 001206 --allow-new-series` runs for a window and that code has entities but no stored rows
- **THEN** one entity is chosen for the code, its source series for the window is written, and the log names the code as a new series

#### Scenario: The flag is absent

- **WHEN** the same code is named without `--allow-new-series`
- **THEN** the run reports it among the codes without stored rows and writes nothing for it

#### Scenario: Row presence still decides between twins

- **WHEN** a code has two candidate entities (`600000` and `600000.SH`) and one of them holds rows
- **THEN** that entity is used whether or not `--allow-new-series` is passed, so a single series is never split

#### Scenario: Universe runs cannot grow the family

- **WHEN** `--universe` is used, with or without the flag
- **THEN** only codes whose entities already hold rows are in scope
