"""Regression coverage for the reproducible bulk-snapshot catalog."""
from __future__ import annotations

from fd_open_data_mcp.scripts import mark_bulk_snapshot


def test_fund_manager_em_is_a_snapshot_command():
    assert mark_bulk_snapshot.SNAPSHOT_COMMANDS.count("fund_manager_em") == 1


def test_snapshot_marker_is_shipped_inside_package():
    assert mark_bulk_snapshot.__name__ == "fd_open_data_mcp.scripts.mark_bulk_snapshot"
