"""One process, two surfaces: /panel/* routes to the console panel, the
rest to the MCP endpoint (crawl-platform task 4 deployment shape)."""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from fd_open_data_mcp import db as dbmod
from fd_open_data_mcp.models import Base


@pytest.fixture
def panel_db(tmp_path, monkeypatch):
    monkeypatch.setenv("FD_OPEN_DATA_MCP_DATABASE_URL",
                       f"sqlite:///{tmp_path / 'composite.db'}")
    dbmod.reset_database()
    Base.metadata.create_all(dbmod.get_database().engine)
    yield
    dbmod.reset_database()


def _asgi_with_env(monkeypatch, *, token="", serve_panel=None):
    os.environ.pop("MCP_BEARER_TOKEN", None)
    if token:
        os.environ["MCP_BEARER_TOKEN"] = token
    if serve_panel is not None:
        os.environ["SERVE_PANEL"] = serve_panel
    else:
        os.environ.pop("SERVE_PANEL", None)
    from fd_open_data_mcp import server
    return server._http_asgi()


def test_panel_served_alongside_mcp(panel_db, monkeypatch):
    os.environ.pop("PANEL_TOKEN", None)
    client = TestClient(_asgi_with_env(monkeypatch))
    r = client.get("/panel", follow_redirects=False)
    assert r.status_code in (200, 302, 303)  # the panel gate answers, not a stray 404


def test_serve_panel_opt_out(monkeypatch):
    client = TestClient(_asgi_with_env(monkeypatch, serve_panel="0"))
    r = client.get("/panel", follow_redirects=False)
    assert r.status_code == 404  # panel not mounted; MCP app's own 404


def test_mcp_bearer_still_guards_mcp(panel_db, monkeypatch):
    client = TestClient(_asgi_with_env(monkeypatch, token="s3cret"))
    r = client.post("/mcp", json={})
    assert r.status_code in (401, 403)
    # panel branch is not behind the MCP bearer (it has its own gate)
    os.environ.pop("PANEL_TOKEN", None)
    r2 = client.get("/panel", follow_redirects=False)
    assert r2.status_code in (200, 302, 303, 401)
