"""Authenticated-crawling identity-pool MCP tools (session-pool, task 4.2).

Exposes the identity pool (``crawl_identities`` / ``crawl_identity_events``)
as FastMCP tools, following the ``register_*`` pattern of platform_tools:

- ``auth_status`` — pool overview: per-source identity counts + the five-state
  status distribution (and the in-flight login-station count,
  login-station-console 3.4); detail rows when one source is asked for,
- ``auth_events`` — the identity event stream (newest first),
- ``auth_request_login`` — register / reset an identity to ``login_required``
  (the 需登录队列) so an operator (or agent) picks it up on the login site,
- ``auth_launch_login`` — ensure the identity (+ egress) and launch a login
  station (headful browser pod + panel-gated observation channel); the login
  itself still happens inside the station (spec authenticated-crawling:
  登录经登录站完成且观察通道仅经 Console 反代可达).

Reads use the same aggregations the Console auth panel renders
(visibility.snapshot identity_* functions) so the two surfaces agree by
construction. Row writes only insert/update rows + ``note`` events; the one
launch write goes through station_ops (Job+Service creation is delegated to
the injectable station client).
"""
from __future__ import annotations

import datetime as dt
import logging

from fastmcp import FastMCP
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from fd_open_data_mcp import station_ops
from fd_open_data_mcp.db import get_database
from fd_open_data_mcp.models import CrawlIdentity, CrawlIdentityEvent
from fd_open_data_mcp.visibility import snapshot as _snapshot

logger = logging.getLogger(__name__)

AUTOMATION_LEVELS = ("auto", "assisted")


# ── shared write operation ────────────────────────────────────────────────────
def request_identity_login(
    session: Session,
    source: str,
    account_alias: str,
    automation: str = "assisted",
    requested_by: str = "mcp",
    now: dt.datetime | None = None,
) -> dict:
    """Register (or reset) an identity as ``login_required``.

    New identity: INSERT (status=login_required, automation). An existing row
    is only RESET — status -> login_required and the lease cleared (a
    login-required identity must not appear leased) — every other column is
    left alone (the INSERT ... ON CONFLICT DO NOTHING semantics; races lose to
    the UNIQUE(source, account_alias) winner and continue on that row). Either
    way one ``note`` identity_event is appended — the request itself is part
    of the audit trail.

    Returns:
      {"status": "queued", "identity_id", "source", "account_alias",
       "created": bool, "previous_status": str | None, "note"}
      {"status": "invalid", "reason"} — automation not in (auto, assisted)
    """
    if automation not in AUTOMATION_LEVELS:
        return {"status": "invalid",
                "reason": (f"automation must be one of {AUTOMATION_LEVELS}, "
                           f"got '{automation}'")}
    now = now or dt.datetime.now(dt.timezone.utc)
    existing = (session.query(CrawlIdentity)
                .filter_by(source=source, account_alias=account_alias).first())
    created = False
    if existing is None:
        row = CrawlIdentity(source=source, account_alias=account_alias,
                            status="login_required", automation=automation)
        session.add(row)
        try:
            session.flush()
        except IntegrityError:
            # a concurrent registration won the UNIQUE(source, account_alias):
            # adopt its row and continue (ON CONFLICT DO NOTHING semantics)
            session.rollback()
            existing = (session.query(CrawlIdentity)
                        .filter_by(source=source,
                                   account_alias=account_alias).first())
        else:
            existing = row
            created = True
    if existing is None:  # pragma: no cover - only if the race row vanished
        return {"status": "error", "reason": "identity insert failed"}
    if created:
        previous = None
    else:
        # reset: status -> login_required, lease cleared; every other column
        # is left alone (INSERT ... ON CONFLICT DO NOTHING semantics)
        previous = existing.status
        existing.status = "login_required"
        existing.lease_owner = None
        existing.lease_token = None
        existing.lease_expires_at = None
    session.add(CrawlIdentityEvent(
        identity_id=existing.id, kind="note",
        detail=(f"login requested by {requested_by} "
                f"(automation={automation}; previous status: "
                f"{previous if previous is not None else 'new'})"),
        created_at=now))
    session.commit()
    logger.info("login requested for %s/%s by %s (created=%s, previous=%s)",
                source, account_alias, requested_by, created, previous)
    return {"status": "queued", "identity_id": existing.id,
            "source": source, "account_alias": account_alias,
            "created": created, "previous_status": previous,
            "note": ("queued in the login-required list; the login itself "
                     "happens on the login site — this tool only writes rows")}


def launch_login_station(
    session: Session,
    source: str,
    account_alias: str,
    automation: str = "assisted",
    requested_by: str = "mcp",
    client=None,
) -> dict:
    """Shared launch write (login-station-console 3.4): ensure the identity
    (login_required + egress) then create the station. Returns the
    create_station dict (status launched / failed / not_found) with the
    identity fields merged in; ``vnc_url`` is RELATIVE — the observation
    channel is behind the panel gate, so no credential is ever embedded."""
    ensured = station_ops.ensure_identity_with_egress(
        session, source, account_alias, automation=automation,
        requested_by=requested_by)
    if ensured.get("status") != "queued":
        return ensured
    out = station_ops.create_station(session, source, account_alias,
                                     client=client)
    if out.get("status") == "launched":
        out["vnc_url"] = out.get("vnc_path")
        out["note"] = ("station launched; complete the login in the panel "
                       "observation view (vnc_url is relative — open it "
                       "through the panel with your own credential; the "
                       "station is not reachable any other way)")
    return {**{k: ensured.get(k) for k in ("identity_id", "egress_ref",
                                           "proxy_url", "created",
                                           "previous_status")}, **out}


def register_auth_tools(mcp: FastMCP) -> None:
    """Attach the identity-pool tools to the given FastMCP instance."""

    def _session() -> Session:
        return get_database().get_session()

    @mcp.tool
    def auth_status(source: str | None = None) -> dict:
        """Authenticated-crawling identity-pool overview.

        Per source: identity count, the five-state status distribution
        (login_required / active / cooldown / banned / retired) and the
        currently-leased count; plus pool-level totals and the in-flight
        login-station count (``stations.active`` — launching / waiting for
        operator; login-station-console). With ``source``: also the detail
        rows for that source's identities (account, status, automation, last
        login/success, zero-run streak, lease view) — an unknown source
        returns zero totals and empty rows, not an error. Read-only.

        Args:
            source: restrict to one source (adds the detail rows).
        """
        s = _session()
        try:
            out = _snapshot.identity_pool_summary(s, source=source)
            out["stations"] = station_ops.active_stations_summary(s)
            return out
        finally:
            s.close()

    @mcp.tool
    def auth_events(source: str | None = None, limit: int = 20) -> list[dict]:
        """Identity event stream (crawl_identity_events), newest first: the
        login/probe/lease audit trail with the identity (source + account
        alias) attached. Same projection the Console auth panel renders.

        Args:
            source: filter to one source's identities.
            limit: max rows (default 20, hard cap 200).
        """
        s = _session()
        try:
            return _snapshot.identity_events(s, source=source, limit=limit)
        finally:
            s.close()

    @mcp.tool
    def auth_request_login(source: str, account_alias: str,
                           automation: str = "assisted") -> dict:
        """Register or reset an identity as login_required (the 需登录队列).

        Only writes ``crawl_identities`` + a ``note`` identity_event — the
        login itself happens on the login site (headful, via the observation
        channel); this tool never executes it. An existing identity is reset
        (status -> login_required, lease cleared); a new one is registered
        with the given automation level (auto | assisted).

        Args:
            source: the source the identity belongs to.
            account_alias: the account alias in that source's pool.
            automation: login unit's declared level (default "assisted").
        """
        s = _session()
        try:
            return request_identity_login(s, source, account_alias,
                                          automation=automation,
                                          requested_by="mcp")
        finally:
            s.close()

    @mcp.tool
    def auth_launch_login(source: str, account_alias: str,
                          automation: str = "assisted") -> dict:
        """Launch a login station for an identity (login-station-console).

        Registers/resets the identity as login_required with an auto-assigned
        egress proxy, then launches the station (a headful-browser Job + the
        panel-gated noVNC observation channel). The login itself happens
        inside the station: the operator completes slider/captcha in the
        panel's embedded view at ``vnc_url`` (RELATIVE — open it through the
        panel with your own credential; no token is embedded and the station
        is reachable only through the panel). Station status then advances
        launching -> waiting_operator -> completed; see ``auth_status``.

        Args:
            source: the source the identity belongs to.
            account_alias: the account alias in that source's pool.
            automation: login unit's declared level (default "assisted").
        """
        s = _session()
        try:
            return launch_login_station(s, source, account_alias,
                                        automation=automation,
                                        requested_by="mcp")
        finally:
            s.close()
