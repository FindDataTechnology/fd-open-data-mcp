r"""Roll federated runner_image pins onto the anchored legal-line tag.

Revision ID: 0011_federated_runner_tag
Revises: 0010_panel_action_audit

2026-10-14 incident follow-up. ``crawl_sources.runner_image`` is the image the
dispatcher bakes into every dispatched Job for a federated member — it is
*not* covered by ``dispatch.sync_sources`` (that sync owns only
site/schedule/enabled/auth_profile/last_commit, and skips federated rows
entirely), so whatever 0007 seeded stays until someone rolls it by hand.

By 2026-10-14 the rows had drifted onto two tags that no longer resolve for
the pull path:

* ``…fd-law-data:sha-3ca3e8b`` on the six legal crawls — present on hkccr but
  **evicted from ccr**, which is the only registry the nodes pull from. Every
  dispatched run was one node image-GC away from ImagePullBackOff.
* ``…fd-law-data:sha-9eb5c72`` on rmfyalk-case-crawl — present in **neither**
  registry (built by a server2 delta that never reached hkccr), so a
  dispatched rmfyalk run could not start at all.
* 0007's own seed constants had the same disease
  (``_IMG_FDK = sha-8433e3d``, ``_IMG_BROKER = sha-4b79ec5`` — the latter
  exists nowhere), so a freshly-migrated database would reproduce it.

This migration converges every federated member that declares a runner image
onto the legal line's anchored tag, which is what the chart-managed CronJobs
run and what cheap3's relay re-asserts to ccr every five minutes
(``/etc/tcr-relay/anchors.conf``). The rule the incident produced: **any image
a dispatcher can spawn must exist on ccr and be anchored** — hkccr-resident
or hand-bumped tags are time bombs.

Production was repaired by hand on 2026-10-14 (pre-image in
``fd-law-data/docs/incidents/2026-10-14-imagepullbackoff.md``); this migration
is the durable, replayable copy of that roll. It is a no-op on an already-rolled
database. Downgrade is a no-op: the drifted tags must not be restored.

PostgreSQL-only like the rest of the chain (SQLite test consumers build from
the models with ``create_all``).
"""
from __future__ import annotations

from alembic import op

revision = "0011_federated_runner_tag"
down_revision = "0010_panel_action_audit"
branch_labels = None
depends_on = None

# The legal line's anchored tag (ccr, mirrored to hkccr, in the relay anchors).
_ANCHORED_TAG = (
    "ccr.ccs.tencentyun.com/lawcraw-business/fd-law-data:sha-b63d0d1"
)

# Rows that are known to have carried the dead pins. Scoped by source name so a
# deliberately-pinned row outside this set is never touched.
_DRIFTED = (
    "ccdi-supervisory-crawl",
    "flk-law-crawl",
    "gov-rules-crawl",
    "guide-cases-crawl",
    "mfa-treaty-crawl",
    "party-regulation-crawl",
    "rmfyalk-case-crawl",
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return  # sqlite tests build via create_all (chain convention)
    for source in _DRIFTED:
        bind.exec_driver_sql(
            "UPDATE crawl_sources SET runner_image = %s, updated_at = now() "
            "WHERE source = %s AND kind = 'federated' "
            "AND runner_image IS DISTINCT FROM %s",
            (_ANCHORED_TAG, source, _ANCHORED_TAG),
        )


def downgrade() -> None:
    # Intentionally empty: the pre-image tags no longer resolve for the pull
    # path, so restoring them would re-arm the incident.
    return
