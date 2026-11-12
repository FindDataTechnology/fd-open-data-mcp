## ADDED Requirements

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
