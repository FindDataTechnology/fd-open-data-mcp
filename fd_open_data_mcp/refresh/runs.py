"""Run control: cancel an open crawl run + ad-hoc launches (panel-ops-console).

Cancellation is a CAS update first, executor teardown second: the row's
status is the single source of truth, so a cancel racing a natural finish can
never orphan a running row, and a Job that is already gone is not an error.
The reconciler treats `cancelled` as terminal — it never re-probes, never
re-launches, and never counts a cancelled row toward cluster capacity.

Ad-hoc launches run a compiled plan WITHOUT a persistent policy row: the run
is recorded with ``policy_id=NULL, origin='adhoc'``. Single-flight is
per-policy, so an ad-hoc run can never be spuriously blocked by — or block —
a policy run; the plan-size ceiling is the only guardrail (design D3).
"""
from __future__ import annotations

import datetime as dt
import logging
from types import SimpleNamespace

from sqlalchemy.orm import Session

from fd_open_data_mcp.models import PolicyRun
from fd_open_data_mcp.refresh.reconciler import (
    CANCELLED, POLICY_MAX_FETCHES, Launcher, _OPEN, estimate_fetches,
)

logger = logging.getLogger(__name__)


def cancel_run(
    session: Session,
    run_id: int,
    actor: str | None = None,
    launcher: Launcher | None = None,
    now: dt.datetime | None = None,
) -> dict:
    """Cancel an open run: CAS status -> delete the executor Job (best effort).

    Returns a dict with one of:
      {"status": "cancelled", "job_ref", "job_deleted"} — the CAS won; the Job
          delete was attempted when a launcher was supplied (job_deleted says
          whether a running executor was actually torn down).
      {"status": "already_finished", "current_status"} — the CAS lost; the row
          was closed by someone else first. No side effects.
      {"status": "not_found"} — no such run id.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)

    detail = f"cancelled by {actor}" if actor else "cancelled"
    updated = (
        session.query(PolicyRun)
        .filter(PolicyRun.id == run_id, PolicyRun.status == _OPEN)
        .update({"status": CANCELLED, "finished_at": now,
                 "cancelled_by": actor, "detail": detail},
                synchronize_session=False)
    )
    if updated == 0:
        run = session.get(PolicyRun, run_id)
        if run is None:
            return {"status": "not_found"}
        logger.info("cancel of run %d rejected: already %s", run_id, run.status)
        return {"status": "already_finished", "current_status": run.status}

    session.commit()
    logger.info("run %d cancelled by %s", run_id, actor or "(panel)")

    job_deleted = False
    run = session.get(PolicyRun, run_id)
    job_ref = run.job_ref if run else None
    if launcher is not None and job_ref:
        try:
            job_deleted = bool(launcher.delete(job_ref))
        except Exception:  # noqa: BLE001 - row status stays the truth (D2)
            logger.exception("job delete failed for cancelled run %d (%s)", run_id, job_ref)
    return {"status": "cancelled", "run_id": run_id,
            "job_ref": job_ref, "job_deleted": job_deleted}


# ─── ad-hoc one-off crawls (design D3) ───────────────────────────────────────
_ADHOC_POLICY = SimpleNamespace(
    id="adhoc", name="adhoc", executor="scrapy", script=None, script_args=None,
    source_filter=None,
)
"""Policy-like stub for launchers: jobs are named ``crawl-policy-adhoc-<ts>``
and cluster-manifest labels carry ``policy-id=adhoc``. Launchers touch nothing
else on the policy in the scrapy path."""


def launch_adhoc(
    session: Session,
    plan,
    launcher: Launcher,
    now: dt.datetime | None = None,
) -> dict:
    """Launch a one-off crawl from a compiled plan without creating a policy.

    Same plan-size ceiling as policy runs (POLICY_MAX_FETCHES); a refusal is
    recorded as a failed ad-hoc run row so it is visible in the runs views.
    No single-flight: overlapping ad-hoc and policy runs are safe because
    observation upserts are idempotent.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)

    plan_json = plan.model_dump(mode="json")
    estimate = estimate_fetches(session, plan)
    if estimate > POLICY_MAX_FETCHES:
        detail = (f"refused: estimated {estimate} fetches exceeds "
                  f"POLICY_MAX_FETCHES={POLICY_MAX_FETCHES}")
        logger.warning("adhoc launch refused: %s", detail)
        session.add(PolicyRun(policy_id=None, origin="adhoc", status="failed",
                              plan_json=plan_json, started_at=now,
                              finished_at=now, detail=detail))
        session.commit()
        return {"status": "refused", "estimate": estimate, "reason": detail}

    job_ref, cluster_id = launcher.launch(plan, _ADHOC_POLICY)
    session.add(PolicyRun(policy_id=None, origin="adhoc", status=_OPEN,
                          plan_json=plan_json,
                          plan_cells=getattr(plan, "plan_cells", None),
                          job_ref=job_ref, cluster_id=cluster_id,
                          started_at=now))
    session.commit()
    logger.info("adhoc launched job=%s estimate=%d", job_ref, estimate)
    return {"status": "launched", "job_ref": job_ref, "estimate": estimate}
