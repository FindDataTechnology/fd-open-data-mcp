# Security Policy

## Supported versions

| Version | Supported |
| ------- | --------- |
| latest  | ✅        |

Security updates are released for the current version only.

## Reporting a vulnerability

Please report suspected vulnerabilities **privately**, not through a public
issue:

- Email: **security@finddatatech.cloud**
- Include: affected version, a description, and a minimal reproduction if
  possible.

We aim to acknowledge within 3 business days and to ship a fix or a documented
mitigation within 30 days for confirmed high-severity issues. We will coordinate
disclosure timing with you.

## Threat model notes for this project

fd-open-data-mcp is a data-acquisition engine that talks to many upstream
sources and, in production, runs crawler workers on remote clusters. The
security-relevant surfaces:

- **Credentials.** API keys and database URLs come from the environment
  (`POLYGON_API_KEY`, `EDGAR_IDENTITY`, `FD_OPEN_DATA_MCP_DATABASE_URL`,
  `REDIS_URL`, …). Never commit them; the codebase ships with a sanitization
  gate (`scripts/conformance_gate.py` / the OSS sanitizer) that fails on
  credential patterns. Treat any committed secret as leaked and rotate it.
- **The panel.** `/panel` is an operations console. It is gated by OIDC
  (Logto) roles or a `PANEL_TOKEN`; with neither configured it ships-dark
  (open, no RBAC) — **do not expose an unconfigured panel publicly.**
- **Crawler egress.** Workers fetch arbitrary upstream URLs. Respect each
  source's terms of service and robots/rate-limit signals; the engine enforces
  politeness rate limits but cannot judge a source's legal access.
- **Federated reads.** Reads may route to other MCP services over the mesh;
  those are read-only and fail loud when the peer is unreachable.

## Hardening checklist for deployments

- Run the panel behind authentication (set `LOGTO_*` or `PANEL_TOKEN`).
- Bind Postgres/Redis to private networks; the default dev SQLite is local-only.
- Pin worker images by digest; keep the schema gate on (no runtime DDL).
- Rotate any datasource key that has ever appeared in a log or commit.
