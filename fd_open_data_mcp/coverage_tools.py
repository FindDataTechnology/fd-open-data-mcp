"""Coverage MCP tools (expand-crawl-coverage).

``coverage_report`` returns the same data as ``fd-open-data-mcp coverage``:
the aggregated covered-vs-routable summary plus optionally the per-concept
gap rows, and the active/most-recent coverage wave state. Read-only.
"""
from __future__ import annotations

from fastmcp import FastMCP

from fd_open_data_mcp.db import get_database


def register_coverage_tools(mcp: FastMCP) -> None:
    """Attach the coverage tools to the given FastMCP instance."""

    @mcp.tool
    def coverage_report(
        entity_type: str | None = None,
        detail: bool = False,
    ) -> dict:
        """Crawl-coverage gap inventory (read-only).

        Args:
            entity_type: Filter to one entity type (fund/stock/country/...).
            detail: Include per-concept rows (default: summary only).

        Returns:
            Summary: covered vs routable per entity_type, plus the active or
            most recent coverage wave and its rows_new. With detail=True,
            per-concept routability / watermark / staleness rows.
        """
        from fd_open_data_mcp.coverage.inventory import (
            coverage_inventory, coverage_summary,
        )
        from fd_open_data_mcp.models import CoverageWave

        s = get_database().get_session()
        try:
            out = coverage_summary(s)
            wave = (s.query(CoverageWave)
                    .filter(CoverageWave.status.in_(("planned", "running", "verifying")))
                    .order_by(CoverageWave.id.desc()).first())
            if wave is None:
                wave = (s.query(CoverageWave)
                        .order_by(CoverageWave.id.desc()).first())
            out["wave"] = wave.toDict() if wave else None
            if detail:
                out["concepts"] = coverage_inventory(s, entity_type=entity_type)
            return out
        finally:
            s.close()
