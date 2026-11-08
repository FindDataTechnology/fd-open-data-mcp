# source-credential-hygiene Specification

## ADDED Requirements

### Requirement: A database credential SHALL NOT appear on a command line

A launched Job's command SHALL NOT contain a database DSN. Direct scripts SHALL
resolve the DSN from the environment (`FD_OPEN_DATA_MCP_DATABASE_URL`) when the
`--db-url` flag is absent, SHALL accept the flag for deliberate local runs, and
SHALL fail with an explicit message when neither is available.

#### Scenario: A launched Job carries no DSN in its command

- **WHEN** the reconciler builds a direct-script Job
- **THEN** the container command contains only the script path and the policy's script args, and the DSN arrives via the environment

#### Scenario: A script falls back to the environment

- **WHEN** a direct script runs without `--db-url` but with `FD_OPEN_DATA_MCP_DATABASE_URL` set
- **THEN** it connects using that value

#### Scenario: Neither source available

- **WHEN** a direct script runs with no `--db-url` and no env var
- **THEN** it exits with an explicit message naming both options, before any network call
