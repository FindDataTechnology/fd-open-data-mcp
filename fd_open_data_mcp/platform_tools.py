"""Crawl-platform control MCP tools (crawl-platform, tasks 4.2/4.3).

Exposes the platform source inventory, run history and the control operations
(trigger / cancel run / cancel pending) as FastMCP tools, following the
``register_*`` pattern of policy_tools. The write operations are plain
module-level functions shared with the panel routes, so the console and the
MCP entrance execute literally the same validation + CAS semantics against
``crawl_sources`` / ``crawl_runs`` / ``pending_runs`` (spec crawl-control-plane:
one operator set, two entrances).

Guardrails shared with the panel (spec: MCP 触发等价于 Console 触发):

- unregistered sources are refused (crawl_sources is the registry),
- disabled sources are refused,
- federated members (kind='federated') trigger by declaration: frozen rows
  and rows without a complete runner declaration are refused; platform rows
  without a manifest mirror are refused (EXIT_SOURCE_MISSING guardrail),
- single-flight: a source with an open ``crawl_runs`` row is refused,
- cancel of a run only SETS ``cancel_requested`` on a still-running row (CAS);
  terminal rows return an explanation instead of an error,
- cancel of a pending row is CAS on status in (pending, claimed).

Reads use the same aggregations the panel renders (visibility.snapshot
platform_* functions) so the two surfaces agree by construction.
"""
from __future__ import annotations

import datetime as dt
import logging

from fastmcp import FastMCP
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from fd_open_data_mcp.db import get_database
from fd_open_data_mcp.models import CrawlRun, CrawlSource, PendingRun
from fd_open_data_mcp.visibility import snapshot as _snapshot

logger = logging.getLogger(__name__)


# ── shared control operations (panel routes call these too) ──────────────────
def trigger_platform_run(
    session: Session,
    source: str,
    requested_by: str = "panel",
    params: dict | None = None,
    now: dt.datetime | None = None,
) -> dict:
    """Insert a ``pending_runs`` row for ``source`` after validation.

    Returns one of:
      {"status": "triggered", "pending_id", "source", "site"}
      {"status": "not_found", "reason"} — source not registered in crawl_sources
      {"status": "refused", "reason"} — disabled source, a frozen federated
          member, a federated member without a runner declaration, a platform
          source with no manifest mirror, or an open run exists (single-flight)
      {"status": "error", "reason"} — the insert itself was rejected (e.g. the
          source's site is not registered in crawl_sites); friendly text, the
          caller never sees a raw IntegrityError
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    src = session.get(CrawlSource, source)
    if src is None:
        return {
            "status": "not_found",
            "reason": (f"source '{source}' is not registered in crawl_sources "
                       f"(the registry holds manifest-mirrored platform "
                       f"sources and seeded federated members); "
                       f"trigger refused"),
        }
    if not src.enabled:
        return {
            "status": "refused",
            "reason": f"source '{source}' is disabled in crawl_sources; "
                      f"enable it before triggering",
        }
    if src.kind == "federated":
        # legal-line-federation: federated members trigger BY DECLARATION.
        # The dispatcher executes the registered runner image/command
        # verbatim, so a complete declaration is the release condition —
        # the content-repo manifest check below does not apply to them.
        if src.frozen_reason:
            return {
                "status": "refused",
                "reason": (f"source '{source}' is frozen: "
                           f"{src.frozen_reason}; clear frozen_reason in "
                           f"crawl_sources before triggering"),
            }
        if not src.runner_declared:
            return {
                "status": "refused",
                "reason": (f"source '{source}' is a federated member without "
                           f"a complete runner declaration (runner_image/"
                           f"runner_command NULL) — the site dispatcher has "
                           f"nothing to execute; fix the registration first"),
            }
    elif src.last_commit is None:
        # Platform-kind rows are dispatcher-mirrored from spiders/*/manifest.yaml;
        # no mirror = the dispatcher's runner_cli would exit EXIT_SOURCE_MISSING
        # and record a bogus failed run. Refuse with a pointer instead.
        return {
            "status": "refused",
            "reason": (f"source '{source}' has no content-repo manifest "
                       f"mirrored (last_commit NULL) — the platform "
                       f"dispatcher cannot run it; register it as a federated "
                       f"member (kind='federated' + runner declaration) if it "
                       f"executes outside the platform dispatcher"),
        }
    open_run = (
        session.query(CrawlRun.id)
        .filter(CrawlRun.source == source, CrawlRun.status == "running")
        .order_by(CrawlRun.id.desc()).first()
    )
    if open_run is not None:
        return {
            "status": "refused",
            "reason": (f"source '{source}' already has an open run "
                       f"#{open_run[0]} (single-flight); wait for it to finish "
                       f"or cancel it first"),
        }
    row = PendingRun(source=source, site=src.site, params=params or {},
                     requested_by=requested_by or "panel", status="pending")
    session.add(row)
    try:
        session.commit()
    except IntegrityError as e:
        session.rollback()
        logger.warning("trigger of %s rejected by the database: %s", source, e)
        return {
            "status": "error",
            "reason": (f"trigger of '{source}' rejected: its site "
                       f"'{src.site}' is not registered in crawl_sites "
                       f"(foreign key) — register the site first"),
        }
    logger.info("pending run %d queued for source %s by %s",
                row.id, source, requested_by)
    return {"status": "triggered", "pending_id": row.id,
            "source": source, "site": src.site}


def cancel_platform_run(
    session: Session,
    run_id: int,
    now: dt.datetime | None = None,
) -> dict:
    """CAS-set ``cancel_requested`` on a RUNNING ``crawl_runs`` row.

    The control plane never closes the row itself — the runner observes the
    flag at its checkpoints and terminates as ``cancelled``. Terminal rows lose
    the CAS and get an explanation back (mirror of refresh.runs.cancel_run's
    contract, adapted to the flag semantics).
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    updated = (
        session.query(CrawlRun)
        .filter(CrawlRun.id == run_id, CrawlRun.status == "running")
        .update({"cancel_requested": now}, synchronize_session=False)
    )
    if updated:
        session.commit()
        logger.info("cancel requested for platform run %d", run_id)
        return {"status": "cancel_requested", "run_id": run_id,
                "note": "the runner will terminate as cancelled at its next "
                        "checkpoint"}
    run = session.get(CrawlRun, run_id)
    if run is None:
        return {"status": "not_found"}
    return {"status": "already_finished", "run_id": run_id,
            "current_status": run.status,
            "reason": f"run {run_id} already finished ({run.status}); "
                      f"nothing cancelled"}


def cancel_pending_run(
    session: Session,
    pending_id: int,
    now: dt.datetime | None = None,
) -> dict:
    """CAS a ``pending_runs`` row to cancelled; only pending/claimed rows can
    be cancelled (a claimed run may still observe the state before launch —
    the dispatcher re-checks status before executing)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    updated = (
        session.query(PendingRun)
        .filter(PendingRun.id == pending_id,
                PendingRun.status.in_(("pending", "claimed")))
        .update({"status": "cancelled", "finished_at": now},
                synchronize_session=False)
    )
    if updated:
        session.commit()
        logger.info("pending run %d cancelled", pending_id)
        return {"status": "cancelled", "pending_id": pending_id}
    row = session.get(PendingRun, pending_id)
    if row is None:
        return {"status": "not_found"}
    return {"status": "already_finished", "pending_id": pending_id,
            "current_status": row.status,
            "reason": f"pending run {pending_id} already {row.status}; "
                      f"nothing cancelled"}


def register_platform_tools(mcp: FastMCP) -> None:
    """Attach the crawl-platform tools to the given FastMCP instance."""

    def _session() -> Session:
        return get_database().get_session()

    @mcp.tool
    def platform_sources(site: str | None = None) -> dict:
        """Crawl-platform source inventory with health and stall hints.

        Every source mirrored into ``crawl_sources`` (content-repo manifests +
        federation members), each with: site, schedule (null = registered but
        not lit), enabled, the latest ``crawl_runs`` status/time/rows_written,
        the active pending-run count, and a ``stalled`` hint (a lit, enabled
        source with no run record for >7 days). Read-only.

        Args:
            site: restrict the listing to one member site (e.g. "tencent").
        """
        s = _session()
        try:
            rows = _snapshot.platform_sources(s, site=site)
            return {
                "total": len(rows),
                "lit": sum(1 for r in rows if r["schedule"]),
                "stalled": sum(1 for r in rows if r["stalled"]),
                "stalled_days": _snapshot.PLATFORM_STALLED_DAYS,
                "sources": rows,
            }
        finally:
            s.close()

    @mcp.tool
    def platform_runs(source: str | None = None, limit: int = 20) -> list[dict]:
        """Platform run history from ``crawl_runs`` (all member sites), newest
        first, with the pending_runs trigger lineage (requested_by) when the
        run was started from the control plane.

        Args:
            source: restrict to one source name.
            limit: max rows (default 20).
        """
        s = _session()
        try:
            return _snapshot.platform_runs(s, source=source, limit=limit)
        finally:
            s.close()

    @mcp.tool
    def platform_trigger(source: str, limit: int | None = None) -> dict:
        """Trigger an immediate platform run for ``source`` (insert pending_runs).

        Same validation and guardrails as the panel's trigger button: the
        source must be registered in crawl_sources and enabled, and single-
        flight applies (refused while an open run exists). Returns
        {"status": "triggered", "pending_id": ...} or a clear refusal text.

        Args:
            source: source name as registered in crawl_sources.
            limit: optional run param override recorded on the pending row
                (passed through to the dispatcher inside params).
        """
        params = {"limit": limit} if limit is not None else None
        s = _session()
        try:
            return trigger_platform_run(s, source, requested_by="mcp",
                                        params=params)
        finally:
            s.close()

    @mcp.tool
    def platform_cancel_run(run_id: int) -> dict:
        """Request cancellation of a RUNNING platform run (sets the
        cancel_requested flag; the runner terminates as cancelled at its next
        checkpoint). Terminal runs return an explanation, unknown ids
        {"status": "not_found"}."""
        s = _session()
        try:
            return cancel_platform_run(s, run_id)
        finally:
            s.close()

    @mcp.tool
    def platform_cancel_pending(pending_id: int) -> dict:
        """Cancel a pending platform run (pending_runs row). CAS: only rows
        still pending/claimed can be cancelled; terminal rows return an
        explanation, unknown ids {"status": "not_found"}."""
        s = _session()
        try:
            return cancel_pending_run(s, pending_id)
        finally:
            s.close()
