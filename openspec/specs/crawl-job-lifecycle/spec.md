# crawl-job-lifecycle Specification

## Purpose
Runtime bounds and terminal outcomes for the crawler Jobs the control plane
launches. A crawl Job must be killed by Kubernetes when it stops making
progress (not left at `Running` forever), and every way a Job can end —
including the deadline kill — must be read back as a terminal state so the
matching `policy_runs` row closes instead of dangling.

## Requirements

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

The detail SHALL additionally carry the last progress the execution body reported before it was
killed — at minimum the item it was working on and how many it had completed — whenever such
progress exists. Only when no progress was ever recorded may the detail fall back to a pure
"deadline exceeded" string, and that fallback SHALL be marked as reason-opaque so a later gate can
count it separately from a deadline kill that has a location.

The execution body SHALL record that progress on its own, frequently enough that a kill at any
point leaves the most recent item behind, and SHALL record it on receiving a termination signal as
well as after each completed item.

#### Scenario: Polling a deadline-terminated Job

- **WHEN** `poll()` observes a Job whose status conditions include `type=Failed` with `reason=DeadlineExceeded`
- **THEN** it returns a terminal outcome and the associated `policy_runs` row is closed with `detail` containing `deadline exceeded`

#### Scenario: 被杀时带着位置

- **WHEN** 一个 run 在完成第 N 个标的、开始第 N+1 个时被 deadline 杀掉
- **THEN** 关闭时的 `detail` 含有 `deadline exceeded` 与该标的标识
- **AND THEN** 也含有已完成计数（N），使「跑得多慢」可读

#### Scenario: 一点进度都没有才允许不透明

- **WHEN** 一个 run 从未记录过任何进度就被杀掉
- **THEN** `detail` 是纯 deadline 字符串并被标记为 reason-opaque
- **AND THEN** 该标记可被新鲜度闸单独计数，不与「带位置的 deadline」混为一谈

#### Scenario: TTL still reclaims the object afterwards

- **WHEN** a Job has been terminated by its deadline and its run row is closed
- **THEN** `ttlSecondsAfterFinished` (86400 s) continues to be honored, so the Job and its pod are garbage-collected without operator action

### Requirement: A Launch Failure SHALL Be Decided By Object Absence, Not By Exception

When a launcher sends a Job creation request and the request raises (timeout, broken
connection, SSL error), the launcher SHALL re-read the cluster for the Job it had
already named before the request, and SHALL decide the outcome by whether that Job
exists rather than by the fact that an exception was raised.

- If the Job exists and its `creationTimestamp` is within the adoption window of the
  attempt, the launch SHALL be treated as **succeeded**: the launcher SHALL return the
  normal `(job_ref, cluster_id)` pair, SHALL NOT re-raise, and SHALL emit one warning
  naming the cluster, the Job, the original exception text, and the Job's
  `creationTimestamp`.
- If the Job does not exist, the launcher SHALL re-raise the original exception, so the
  caller still records a failed run — and the recorded detail SHALL state that the
  cluster was re-read and the Job was confirmed absent, so an operator can tell a clean
  failure from an unknown one.

Because the Job name carries a second-resolution timestamp and is fixed before the
request is sent, the launcher SHALL NOT retry the creation request: a retry can create
a second Job.

#### Scenario: Read timeout after the Job was created

- **WHEN** the Job creation request raises a read timeout and the Job exists with a `creationTimestamp` inside the adoption window
- **THEN** `launch()` returns the normal `(job_ref, cluster_id)` pair without raising, and a warning records that the response was lost but the Job exists

#### Scenario: Handshake failure where nothing was sent

- **WHEN** the Job creation request fails during the TLS handshake or connection setup, and re-reading finds no such Job
- **THEN** `launch()` re-raises, and the run is recorded as failed with a detail stating the Job was confirmed absent

#### Scenario: Response lost and the Job turns out to be absent

- **WHEN** the creation request raises and re-reading the cluster returns 404 for that Job
- **THEN** the run is still recorded as failed (no silent success), and the detail names the absence check

#### Scenario: A run row never contradicts an existing Job

- **WHEN** a run is closed as failed for a launch error
- **THEN** no Job created by that same attempt remains in the target cluster unaccounted for — either the Job was adopted and the run stays open, or the Job never existed

#### Scenario: No second Job from a retry

- **WHEN** the creation request's response is lost
- **THEN** the launcher does not resend the creation request, so at most one Job is created per launch attempt
