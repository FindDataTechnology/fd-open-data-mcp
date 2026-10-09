# ADR 0002: Three global Logto roles for Scout admission, no per-organization isolation

- **Status**: Accepted (2026-10-10, user-approved during Scout externalization exploration)
- **Context**: planned change `panel-rbac-i18n-refresh` (寻新/Wire Scout external access)
- **Deciders**: user, exploring agent

## Context

Scout (the crawl control panel, fd-open-data-mcp `panel/`) is being opened to
external audiences (customers, auditors, outsourced operators). Admission
moves from the single all-or-nothing `panel-user` role to a three-tier matrix:
`panel-viewer` (read-only data surfaces), `panel-operator` (+ crawl actions),
`panel-admin` (+ proxy, login stations/VNC, cluster capacity).

Logto supports binding roles to organizations. Since the panel already has a
production Logto integration with a customize-jwt `roles` claim and
multi-role sessions, both a global and an org-scoped model were available at
similar implementation cost.

## Decision

Roles are **global** (assigned per user in Logto, claim-claimed at login);
no Logto organization binding and no per-customer data partitioning inside
the panel. Every viewer sees the same company-wide data supply surfaces.

The reason: the panel presents one company-wide crawl estate — there is no
per-customer data dimension to isolate. Org binding would be idle complexity
until scope management (indicator scopes) gains customer-grant semantics,
at which point Logto-side rebinding is a configuration change, not a redesign.

`panel-user` remains accepted as an alias of `panel-operator` for a smooth
migration of existing operators.

## Alternatives considered

1. **Org-bound roles** (per-customer organizations, à la wire-customers):
   rejected — nothing to partition today; adds org lifecycle management to
   every admission decision for zero isolation benefit.
2. **A fourth `panel-guest` tier** (coverage + observatory only, no runs
   list): deferred — the viewer tier already hides run details and fleet
   internals; add the narrower tier only if customer feedback shows runs
   lists leak more than comfort allows.

## Consequences

- Admission review = role assignment review in the Logto console; no panel-side
  per-tenant state exists (consistent with "authorization lives in Logto,
  not in deployment env" from the panel-role-gate change).
- If per-customer views are ever needed, the RBAC middleware's
  route→permission map is the extension point; roles stay coarse, data
  filtering would be a new concern.
