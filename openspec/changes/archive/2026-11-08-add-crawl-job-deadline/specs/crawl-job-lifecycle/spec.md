# crawl-job-lifecycle Specification

## ADDED Requirements

### Requirement: Crawl Jobs SHALL Have a Hard Runtime Ceiling

Every crawl Job built by the reconciler's launchers (`MultiClusterLauncher` for scrapy and direct policies, and the single-cluster `K8sJobLauncher`) SHALL carry `spec.activeDeadlineSeconds`, so a Job whose container stays alive without making progress is terminated by Kubernetes instead of occupying cluster capacity indefinitely.

The ceiling SHALL default to `10800` seconds (3 hours) and SHALL be overridable by the environment variable `SCRAW_JOB_ACTIVE_DEADLINE_SECONDS`. Invalid values (non-integer, zero, negative) SHALL fall back to the default with a warning, and SHALL NOT prevent Job creation.

#### Scenario: Scrapy job spec carries the deadline

- **WHEN** the launcher builds the manifests for a policy with `executor=scrapy`
- **THEN** the Job manifest contains `spec.activeDeadlineSeconds` equal to the configured ceiling (default 10800)

#### Scenario: Direct job spec carries the deadline

- **WHEN** the launcher builds the manifests for a policy with `executor=direct`
- **THEN** the Job manifest contains `spec.activeDeadlineSeconds` equal to the configured ceiling (default 10800)

#### Scenario: Single-cluster launcher spec carries the deadline

- **WHEN** the single-cluster `K8sJobLauncher` builds a Job (no multi-cluster fleet routing)
- **THEN** that Job manifest also contains `spec.activeDeadlineSeconds` equal to the configured ceiling

#### Scenario: Operator overrides the ceiling

- **WHEN** `SCRAW_JOB_ACTIVE_DEADLINE_SECONDS=21600` is set on the control plane
- **THEN** newly created Jobs carry `activeDeadlineSeconds: 21600`

#### Scenario: Invalid override falls back to the default

- **WHEN** `SCRAW_JOB_ACTIVE_DEADLINE_SECONDS` is set to `abc` or `-1`
- **THEN** newly created Jobs carry `activeDeadlineSeconds: 10800` and a warning is logged

#### Scenario: Existing Jobs are untouched

- **WHEN** the ceiling configuration changes while Jobs are already running
- **THEN** those existing Jobs keep their original `activeDeadlineSeconds` (the setting applies only to Jobs created afterwards)

### Requirement: Deadline-Terminated Runs SHALL Close With a Distinguishable Reason

When a Job is terminated because it exceeded its runtime ceiling, the reconciler SHALL close the corresponding `policy_runs` row as failed and record a detail string identifying a deadline termination, so that operators can distinguish a wedged crawler from a slow upstream source.

#### Scenario: Polling a deadline-terminated Job

- **WHEN** `poll()` observes a Job whose status conditions include `type=Failed` with `reason=DeadlineExceeded`
- **THEN** it returns a terminal outcome and the associated `policy_runs` row is closed with `detail` containing `deadline exceeded`

#### Scenario: TTL still reclaims the object afterwards

- **WHEN** a Job has been terminated by its deadline and its run row is closed
- **THEN** `ttlSecondsAfterFinished` (86400 s) continues to be honored, so the Job and its pod are garbage-collected without operator action
