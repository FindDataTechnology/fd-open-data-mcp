"""list-sources must derive status from the integration points, not a table.

The command used to carry a hand-written status per source, with 11 of them
marked "✅ Full support" while nothing (no runner, no adapter, no provider
route) could fetch them, and 3 real sources (``datacommons``/``nbs``/``polygon``)
missing entirely. These assertions make that shape impossible to reintroduce:
every status must be explained by the ``via`` list on the same row.
"""
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from fd_open_data_mcp.adapters import registered as registered_adapters
from fd_open_data_mcp.catalog.providers import PROVIDERS
from fd_open_data_mcp.cli import cli
from fd_open_data_mcp.fetch.instrumentation import direct_sources
from fd_open_data_mcp.fetch.runner import RUNNERS


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    from fd_open_data_mcp import db as dbmod

    monkeypatch.setenv("FD_OPEN_DATA_MCP_DATABASE_URL", f"sqlite:///{tmp_path / 'cli.db'}")
    dbmod.reset_database()
    yield
    dbmod.reset_database()


def _payload():
    r = CliRunner().invoke(cli, ["list-sources"])
    assert r.exit_code == 0, r.output
    body = r.output[r.output.index("{"):]
    return json.loads(body)


def test_status_is_explained_by_via(cli_env):
    """A ✅ row must name a dispatch path; a ❌ row must name nothing."""
    for row in _payload()["sources"]:
        dispatch = {"runner", "direct", "adapter"} & set(row["via"])
        if row["status"] == "✅ registered":
            assert dispatch, f"{row['source']} claims registered with via={row['via']}"
        elif row["status"].startswith("⚠️"):
            assert not dispatch, f"{row['source']} marked catalog-only but has {row['via']}"
            assert "provider" in row["via"], row["source"]
        else:
            assert row["via"] == [], f"{row['source']} marked not wired but has {row['via']}"


def test_every_runner_source_is_listed_and_registered(cli_env):
    """RUNNERS is the dispatch table — nothing in it may be hidden or downgraded."""
    rows = {row["source"]: row for row in _payload()["sources"]}
    for source in RUNNERS:
        assert source in rows, f"runner source {source} is not listed"
        assert rows[source]["status"] == "✅ registered", source


def test_every_direct_and_adapter_source_is_registered(cli_env):
    rows = {row["source"]: row for row in _payload()["sources"]}
    for source in set(direct_sources()) | {s for s, _c in registered_adapters()}:
        assert rows[source]["status"] == "✅ registered", source


def test_provider_without_dispatch_path_is_catalog_only(cli_env):
    """A provider entry declares a source; only a dispatch path makes it fetchable."""
    rows = {row["source"]: row for row in _payload()["sources"]}
    dispatch = set(RUNNERS) | set(direct_sources()) | {s for s, _c in registered_adapters()}
    for source in PROVIDERS:
        if source in dispatch:
            continue
        assert rows[source]["status"].startswith("⚠️"), source


def test_summary_counts_add_up(cli_env):
    payload = _payload()
    summary = payload["summary"]
    assert summary["total"] == len(payload["sources"])
    assert (
        summary["registered"] + summary["catalog_only"] + summary["not_wired"]
        == summary["total"]
    )
