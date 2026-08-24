"""Failure + stale-run scan entrypoint (add-crawl-visibility).

``python -m fd_open_data_mcp.visibility.scan``

Runs as its own k3s CronJob (``10,25,40,55 * * * *`` — 3 min after the
reconciler ticks at ``:07/:22/:37/:52``, so it reads freshest post-tick state).
Reads ``policy_runs`` past the scan watermark, detects newly-failed / newly-
refused / newly-stale runs, dedups each via the Redis alerted-set (each
(run, event) alerted once, 7-day TTL), batches ALL new events in the window
into ONE message, sends it, advances the watermark, and prints a JSON summary.

Read-only: it never mutates ``policy_runs`` (a stale run is flagged, not
closed). A sink failure or missing token never breaks a tick — the watermark
still advances.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import sys

from sqlalchemy import func
from sqlalchemy.orm import Session

from fd_open_data_mcp.db import get_database
from fd_open_data_mcp.models import CrawlPolicy, PolicyRun
from fd_open_data_mcp.visibility import snapshot
from fd_open_data_mcp.visibility.notifiers.factory import get_notifier
from fd_open_data_mcp.visibility import state

logger = logging.getLogger(__name__)

_STALE_MIN = int(os.environ.get("SCRAW_STALE_MINUTES", "90"))


def _classify(run: PolicyRun) -> str | None:
    """Map a terminal run to an alert event class, or None (not alertable)."""
    if run.status == "failed":
        if (run.detail or "").lower().startswith("refused:"):
            return "refused"
        return "failed"
    return None


def scan_once(session: Session, now: dt.datetime | None = None) -> dict:
    """One scan tick. Returns a summary dict (also printed by ``main``)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    notifier = get_notifier()

    summary = {"scanned_at": snapshot._iso(now), "failed": [], "refused": [], "stale": [],
               "notified": False}

    # 1. terminal runs since the watermark (failed / refused)
    wm = state.get_scan_watermark()
    q = (session.query(PolicyRun, CrawlPolicy.name)
         .join(CrawlPolicy, PolicyRun.policy_id == CrawlPolicy.id)
         .filter(PolicyRun.status == "failed"))
    if wm is not None:
        # The DB column is TIMESTAMP WITHOUT TIME ZONE (naive-UTC writer
        # contract), so filter against a naive cutoff to avoid a naive/aware
        # mismatch on Postgres. The watermark itself is stored as a UTC epoch.
        wm_naive = dt.datetime.fromtimestamp(wm, dt.timezone.utc).replace(tzinfo=None)
        q = q.filter(func.coalesce(PolicyRun.finished_at, PolicyRun.started_at) > wm_naive)
    terminal = q.order_by(PolicyRun.started_at.asc()).all()

    # 2. stale runs (running too long) — independent of the watermark, deduped
    stale = snapshot.stale_runs(session, stale_min=_STALE_MIN)

    new_failed: list[dict] = []
    new_refused: list[dict] = []
    for run, pname in terminal:
        event = _classify(run)
        if event is None:
            continue
        if state.already_alerted(run.id, event):
            continue
        rec = {
            "run_id": run.id, "policy": pname, "policy_id": run.policy_id,
            "event": event, "status": run.status,
            "datasources": snapshot._plan_datasources(run.plan_json),
            "job_ref": run.job_ref, "cluster_id": run.cluster_id,
            "started_at": snapshot._iso(run.started_at),
            "finished_at": snapshot._iso(run.finished_at),
            "detail": (run.detail or "")[:160],
        }
        if event == "refused":
            new_refused.append(rec)
        else:
            new_failed.append(rec)

    new_stale: list[dict] = []
    for r in stale:
        if state.already_alerted(r["id"], "stale"):
            continue
        new_stale.append({
            "run_id": r["id"], "policy": r["policy"], "policy_id": r["policy_id"],
            "event": "stale", "age_minutes": r["age_minutes"],
            "datasources": r["datasources"], "job_ref": r["job_ref"],
            "cluster_id": r["cluster_id"], "started_at": r["started_at"],
        })

    summary["failed"] = new_failed
    summary["refused"] = new_refused
    summary["stale"] = new_stale

    # 3. batch all new events into ONE message (ServerChan rate-limit aware)
    n_total = len(new_failed) + len(new_refused) + len(new_stale)
    if n_total > 0:
        title, body = _format_message(new_failed, new_refused, new_stale)
        notifier.send(title, body, level="error")
        summary["notified"] = True
        # mark only AFTER a send attempt so a sink outage doesn't drop alerts
        for rec in new_failed + new_refused:
            state.mark_alerted(rec["run_id"], rec["event"])
        for rec in new_stale:
            state.mark_alerted(rec["run_id"], "stale")

    # 4. advance the watermark to the newest terminal run covered
    if terminal:
        newest = max((r.finished_at or r.started_at) for r, _ in terminal)
        if newest.tzinfo is None:
            newest = newest.replace(tzinfo=dt.timezone.utc)
        state.set_scan_watermark(newest.timestamp())

    return summary


def _format_message(failed: list[dict], refused: list[dict], stale: list[dict]) -> tuple[str, str]:
    n = len(failed) + len(refused) + len(stale)
    title = f"🚨 CRAWL FAILURE · {n} run(s)" if (failed or refused) else f"⏳ CRAWL STALE · {n} run(s)"
    lines: list[str] = []
    for rec in failed:
        ds = " → ".join(rec["datasources"]) or "?"
        lines.append(f" • {rec['policy']}   {ds}   {rec['detail'].splitlines()[0] if rec['detail'] else ''}")
    for rec in refused:
        ds = " → ".join(rec["datasources"]) or "?"
        lines.append(f" • {rec['policy']}   REFUSED   {rec['detail'].splitlines()[0] if rec['detail'] else ''}")
    for rec in stale:
        ds = " → ".join(rec["datasources"]) or "?"
        lines.append(f" • {rec['policy']}   stale {rec['age_minutes']}min   {rec['job_ref'] or ''}")
    body = "\n".join(lines)
    if failed or refused:
        jobs = ", ".join(r["job_ref"] for r in (failed + refused) if r.get("job_ref"))
        body += f"\njob: {jobs}" if jobs else ""
        body += '\n→ ask Claude: "scraw status"'
    return title[:32], body


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    session = get_database().get_session()
    try:
        summary = scan_once(session)
    finally:
        session.close()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    # non-zero exit only if something genuinely broke (not "no events found")
    if summary.get("notified") is None and (summary["failed"] or summary["stale"]):
        sys.exit(1)


if __name__ == "__main__":
    main()
