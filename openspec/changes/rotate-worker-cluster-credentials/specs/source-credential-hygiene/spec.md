## MODIFIED Requirements

### Requirement: A database credential SHALL NOT appear on a command line

A launched Job's command SHALL NOT contain a database DSN. Direct scripts SHALL
resolve the DSN from the environment (`FD_OPEN_DATA_MCP_DATABASE_URL`) when the
`--db-url` flag is absent, SHALL accept the flag for deliberate local runs, and
SHALL fail with an explicit message when neither is available.

The same rule SHALL apply to worker-cluster credentials: a token SHALL reach the
process only through the mounted Secret (`/kubeconfigs/<cluster>.token`), and SHALL
NOT be passed as a command-line argument, written into a Job's pod spec, or logged.
A rotation SHALL therefore never touch the manifest that launches Jobs.

#### Scenario: A launched Job carries no DSN in its command

- **WHEN** the reconciler builds a direct-script Job
- **THEN** the container command contains only the script path and the policy's script args, and the DSN arrives via the environment

#### Scenario: A script falls back to the environment

- **WHEN** a direct script runs without `--db-url` but with `FD_OPEN_DATA_MCP_DATABASE_URL` set
- **THEN** it connects using that value

#### Scenario: Neither source available

- **WHEN** a direct script runs with no `--db-url` and no env var
- **THEN** it exits with an explicit message naming both options, before any network call

#### Scenario: Rotation does not touch Job manifests

- **WHEN** a worker cluster's credential is rotated
- **THEN** only the `worker-kubeconfigs` Secret changes; every Job manifest, CronJob
  template and RBAC object stays byte-identical
- **AND** the next pod started by a CronJob picks up the new value without any
  workload restart, because the credential is re-read from disk on each client construction

## ADDED Requirements

### Requirement: Worker cluster credentials SHALL be rotatable with a bounded lifetime

A worker cluster's credential SHALL have a finite lifetime and SHALL be replaceable
without invalidating any other cluster's credential or any other consumer.

Acceptance SHALL be a paired check performed against the real mount point: the
superseded value SHALL fail authentication and the replacement SHALL succeed.
Neither the exit status of a patch command nor a Job's `Complete` state SHALL count
as evidence — a CronJob with nothing to dispatch reports success without ever
authenticating.

#### Scenario: Rotation is verified at the mount point, not in the patching shell

- **WHEN** a rotation tool has written a new token into `worker-kubeconfigs`
- **THEN** it SHALL start a probe that mounts that Secret at `/kubeconfigs` and issue a
  read against the target cluster's API using the mounted file
- **AND** it SHALL report success only if the request returns a successful response
  with the new value AND the superseded value is rejected

#### Scenario: A silent no-op patch is treated as failure

- **WHEN** the patch command reports success but the Secret still holds the superseded value
- **THEN** the rotation SHALL be treated as failed and SHALL be reported, never as complete

#### Scenario: Revoking a leaked credential does not rely on deleting the Secret

- **WHEN** a worker credential is known to have leaked
- **THEN** revocation SHALL change the ServiceAccount identity (or otherwise make the
  old value unusable) so that re-creating a same-named Secret cannot restore it
- **AND** the rotation SHALL restore dispatcher capability in the same operation,
  so that revocation never leaves the fleet unable to dispatch

### Requirement: A disabled cluster SHALL NOT keep usable credentials mounted

When a cluster's registry row is disabled, its credential SHALL be removed from the
Secret mounted into the control plane, so that a stale but still-valid token cannot be
harvested from a running pod or from the Secret itself.

Re-enabling the cluster SHALL require issuing a fresh credential, and that step SHALL
be documented in the rotation tool's output.

#### Scenario: Disabling a cluster removes its mounted credential

- **WHEN** a cluster is disabled and its credential is pruned
- **THEN** `/kubeconfigs/<cluster>.token` no longer exists in the control-plane pod
- **AND** no dispatch to that cluster is attempted

#### Scenario: Re-enabling requires a fresh credential

- **WHEN** a previously disabled cluster is re-enabled
- **THEN** dispatch fails with an explicit "credential missing, issue one" error rather
  than authenticating with a stale value recovered from an old backup
