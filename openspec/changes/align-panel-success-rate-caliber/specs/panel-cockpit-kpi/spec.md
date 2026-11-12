## ADDED Requirements

### Requirement: Success rate is measured over decidable runs only

The panel home KPI "24h 成功率（可判定）" SHALL be computed as
`success / (success + failed + zero_yield)` over runs finished within the last
24 hours. The denominator SHALL NOT include `running` (not terminal) nor
`no_op` / `redundant` (terminal but carrying no signal: empty plan, or fetched
nothing new). The set of failure statuses SHALL be the same one the alerting
classifier uses (`visibility.scan.ALERT_STATUSES`), so that "what counts as a
failure" has exactly one definition in the codebase.

#### Scenario: Clean zero-new runs do not depress the rate
- **WHEN** the last 24h contain 1 `success` and 1 `no_op`
- **THEN** the reported success rate is 100%, and `no_signal` is 1

#### Scenario: Real failures stay in the denominator
- **WHEN** the last 24h contain 1 `success` and 1 `failed`
- **THEN** the reported success rate is 50%, and `failures` is 1

#### Scenario: Non-terminal runs are never counted
- **WHEN** the last 24h contain 1 `success` and 1 `running`
- **THEN** the reported success rate is 100% and `running` is 1

### Requirement: Nothing is silently dropped from the KPI

Every terminal run inside the 24h window SHALL be accounted for by one of
`success`, `failures`, `no_signal`, or `unknown`. Status values that are
neither success, nor in `ALERT_STATUSES`, nor in `NO_SIGNAL_STATUSES` SHALL be
surfaced as `unknown` and rendered on the page, so that introducing a new
status cannot make runs disappear from the headline numbers.

#### Scenario: An unregistered status is surfaced
- **WHEN** a run with status `partial` finishes within the window
- **THEN** `unknown` is 1 and the page shows a non-zero unknown count
