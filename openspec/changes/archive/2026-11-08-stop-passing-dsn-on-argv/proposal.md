# Stop putting the database DSN on the command line

## Why

`_direct_manifests()` builds every direct-script Job's command as

    /opt/venv/bin/python /src/<script>.py <args> --db-url "$FD_OPEN_DATA_MCP_DATABASE_URL"

so the master DSN — password included — is expanded into the pod's
`spec.containers[0].command` (readable by anyone with `get pods`) and into
`/proc/<pid>/cmdline` inside the container. Logs were redacted in 0.5.46; argv
was the remaining copy. The password is already available to the same container
as an env var from the `fd-db-secret` Secret, which is the correct channel.

## What changes

- Direct scripts read the DSN from `FD_OPEN_DATA_MCP_DATABASE_URL` when
  `--db-url` is omitted; passing the flag still works (local runs, backfills).
- `_direct_manifests()` stops appending `--db-url "$FD_OPEN_DATA_MCP_DATABASE_URL"`
  to the command, so no Job spec or process argv carries the credential.
- A script with neither the flag nor the env fails with a clear message instead
  of a connection error.

## Impact

- Affected specs: new capability `source-credential-hygiene`.
- Affected code: `fd_open_data_mcp/refresh/reconciler.py`
  (`_direct_manifests`), `scripts/bulk_ingest_astock_daily.py` (DSN resolution),
  plus tests. Ships in a patch release and needs an image roll (the reconciler
  lives in the package); the script half ships by ConfigMap.
- Out of scope: other credential smells (e.g. plaintext provider keys in
  `runtime_hints` for the cheap cluster) — recorded as separate debt.
