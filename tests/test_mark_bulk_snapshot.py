"""Regression coverage for the reproducible bulk-snapshot catalog."""
from __future__ import annotations

import importlib.util
from pathlib import Path


_SCRIPT = Path(__file__).parent.parent / "scripts" / "mark_bulk_snapshot.py"
_spec = importlib.util.spec_from_file_location("mark_bulk_snapshot", _SCRIPT)
mark_bulk_snapshot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mark_bulk_snapshot)


def test_fund_manager_em_is_a_snapshot_command():
    assert mark_bulk_snapshot.SNAPSHOT_COMMANDS.count("fund_manager_em") == 1
