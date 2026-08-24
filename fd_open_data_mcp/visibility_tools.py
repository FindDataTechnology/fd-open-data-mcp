"""Crawl-visibility MCP tools (add-crawl-visibility).

Exposes ``crawl_status`` as a FastMCP tool — the on-demand pull channel that
complements the push notifications (scan/digest). It returns the same
datasource-centric snapshot the daily digest produces (both call
``snapshot.build_snapshot``), so asking Claude "what's the scraw doing" yields
an answer consistent with the WeChat digest. Read-only: operates through the
same session factory as the other control-plane tools and mutates nothing.
"""
from __future__ import annotations

from fastmcp import FastMCP
from sqlalchemy.orm import Session

from fd_open_data_mcp.db import get_database
from fd_open_data_mcp.visibility import snapshot


def register_visibility_tools(mcp: FastMCP) -> None:
    """Attach the crawl-visibility tools to the given FastMCP instance."""

    def _session() -> Session:
        return get_database().get_session()

    @mcp.tool
    def crawl_status(
        hours: int = 24,
        run_limit: int = 20,
    ) -> dict:
        """Return a structured snapshot of crawl state (on-demand visibility).

        Answers "what is the scraw doing / what will it crawl" without a manual
        DB query. Datasource-centric, matching the daily digest. Read-only.

        Args:
            hours: Window for per-source fetch outcome counts (default 24).
            run_limit: How many recent policy_runs to return (default 20).

        Returns:
            ``{recent_runs, fleet, stale_runs, per_source_outcome,
            circuit_state, today_scheduled, summary}`` — recent runs with
            status + target datasources; fleet health (enabled, reachable,
            open-runs vs capacity); stale runs (>90 min in 'running'); per-
            ``real_source`` ok/error over the window; circuit state; today's
            scheduled policies projected to target datasources; and a roll-up
            ``summary``.
        """
        s = _session()
        try:
            return snapshot.build_snapshot(s, hours=hours, run_limit=run_limit)
        finally:
            s.close()
