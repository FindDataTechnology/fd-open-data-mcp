"""CLI smoke tests (CliRunner + temp DB)."""
from pathlib import Path

import pytest
from click.testing import CliRunner

from fd_open_data_mcp import db as dbmod
from fd_open_data_mcp.cli import cli

EXAMPLE = Path(__file__).resolve().parents[2] / "fd-open-data-protocol" / "examples" / "example_stock.yaml"


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    monkeypatch.setenv("FD_OPEN_DATA_MCP_DATABASE_URL", f"sqlite:///{tmp_path / 'cli.db'}")
    dbmod.reset_database()
    yield
    dbmod.reset_database()


def _run(args, cli_env):
    return CliRunner().invoke(cli, args)


def test_migrate(cli_env):
    r = _run(["migrate"], cli_env)
    assert r.exit_code == 0
    assert "Initialized" in r.output


def test_import_catalog(cli_env):
    _run(["migrate"], cli_env)
    r = _run(["import-catalog", "akshare"], cli_env)
    assert r.exit_code == 0


def test_consume_concepts(cli_env):
    _run(["migrate"], cli_env)
    r = _run(["consume-concepts"], cli_env)
    assert r.exit_code == 0


def test_propose_bindings(cli_env):
    _run(["migrate"], cli_env)
    _run(["import-catalog", "akshare"], cli_env)
    r = _run(["propose-bindings"], cli_env)
    assert r.exit_code == 0


def test_generate_schedules(cli_env):
    _run(["migrate"], cli_env)
    _run(["consume-concepts"], cli_env)
    r = _run(["generate-schedules"], cli_env)
    assert r.exit_code == 0


def test_register_datasource(cli_env):
    _run(["migrate"], cli_env)
    if not EXAMPLE.exists():
        pytest.skip("example_stock.yaml not found")
    r = _run(["register-datasource", str(EXAMPLE)], cli_env)
    assert r.exit_code == 0


def test_list_cnreport_rules(cli_env):
    _run(["migrate"], cli_env)
    r = _run(["list-cnreport-rules"], cli_env)
    assert r.exit_code == 0


def test_register_discovered(cli_env):
    _run(["migrate"], cli_env)
    r = _run(["register-discovered"], cli_env)
    assert r.exit_code == 0
