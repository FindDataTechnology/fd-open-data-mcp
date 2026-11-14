r"""Re-anchor federated runner pins onto the legal line's current tag.

Revision ID: 0024_federated_runner_pin_c33bf67
Revises: 0023_re_fetch_cost

2026-11-14 drift, same disease as 0011 (read that one first): ``crawl_sources
.runner_image`` is the image the dispatcher bakes into every dispatched Job for
a federated member, and **nothing keeps it aligned** — ``dispatch.sync_sources``
skips federated rows entirely, so the value is only ever what someone typed.

0011 converged the rows onto ``sha-b63d0d1`` (the anchored tag at the time).
By 2026-11-14 all seven rows read ``sha-e9c1c11`` (the http.js
guardBodyDeadline build) while the chart and the live CronJobs had moved on to
``sha-c33bf67``. There is no commit anywhere that records the b63d0d1 ->
e9c1c11 hand-bump: it was typed into the table. That is the failure mode this
family keeps producing — a copy with no owner and no diff.

Consequence is not an outage: a dispatched run would simply execute five
generations-old code (missing the image_tag self-report, the S3 write budget,
and the report coverage field). It stays latent only because the site
dispatcher currently handles 0 runs per tick.

Rule unchanged from 0011: **any image a dispatcher can spawn must exist on ccr
and be anchored** (``/etc/tcr-relay/anchors.conf``). ``sha-c33bf67`` is what the
chart pins and what the relay re-asserts.

The gate that makes the next drift visible is in the chart repo:
``helm-law-scraw/scripts/verify-federated-runner-pins.py``
(``make verify-pins ROWS=…``). It compares this column against the chart's own
render and names both tags on divergence — run it before touching either side.

Production was repaired by hand on 2026-11-14 (same UPDATE, run through a
one-shot query Pod, verified afterwards by re-reading the column); this
migration is the durable, replayable copy of that roll. It is a no-op on an
already-converged database. Downgrade is a no-op: the drifted value must not be
restored.

PostgreSQL-only like the rest of the chain (SQLite test consumers build from the
models with ``create_all``).
"""
from __future__ import annotations

from alembic import op

revision = "0024_federated_runner_pin_c33bf67"
down_revision = "0023_re_fetch_cost"
branch_labels = None
depends_on = None

# The legal line's anchored tag, as rendered by helm-law-scraw (images.legalSources
# and the rmfyalk-case-library override both point here).
_ANCHORED_TAG = (
    "ccr.ccs.tencentyun.com/lawcraw-business/fd-law-data:sha-c33bf67"
)

# Same source set as 0011: the rows that declare a fd-law-data runner. Scoped by
# name so a deliberately-pinned row outside this set is never touched. wenshu-crawl
# is deliberately absent (no runner_image yet).
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
    # Intentionally empty: restoring a tag that the chart no longer pins would
    # re-arm the drift (and 0011 took the same position on its own predecessor).
    return
