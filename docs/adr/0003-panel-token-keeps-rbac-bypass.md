# ADR 0003: PANEL_TOKEN retains its full RBAC bypass

- **Status**: Accepted (2026-10-10, user-approved during Scout externalization exploration)
- **Context**: planned change `panel-rbac-i18n-refresh` (寻新/Wire Scout external access)
- **Deciders**: user, exploring agent

## Context

`PANEL_TOKEN` admits programmatic requests to every panel route, bypassing
the OIDC login and — once the three-tier RBAC matrix lands — bypassing it
entirely, i.e. token holders are effectively admin. Existing automation and
operational scripts depend on this single-static-env-token channel. A future
reader of the middleware will see a carefully enforced permission matrix
next to a token that ignores it and reasonably suspect a vulnerability.

## Decision

Keep `PANEL_TOKEN` as a full-bypass programmatic channel. It is an
internal-automation credential: never distributed to external Scout
audiences, and interactive users can never obtain it. RBAC governs
human/OIDC sessions only.

The new operation-audit log records token-authenticated actions distinctly
(actor = `token`) so bypass usage is observable, not silent.

## Alternatives considered

1. **Tiered tokens** (one token per role): rejected for this change — no
   automation consumer needs less than admin today; token splitting adds
   secret-management surface without a consumer.
2. **Dropping the token entirely** (all access via OIDC): rejected — breaks
   headless operational scripts and the ships-dark/local-dev mode where no
   Logto env exists.

## Consequences

- Secret hygiene of `PANEL_TOKEN` is the trust root for programmatic access;
  rotation procedure is a deliberate follow-up (not covered by this ADR).
- If token-authenticated traffic ever needs to be constrained, the audit log
  provides the evidence base to decide which tiers to split.
