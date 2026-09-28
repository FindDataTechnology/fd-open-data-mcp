"""Idle Streamable-HTTP sessions must be reaped before the mcp SDK's hard
10_000-session cap refuses every new session (production incident 2026-09-26:
the gateway hub hit the cap and 503'd all new sessions for a day).

The SDK reaps idle sessions when ``session_idle_timeout`` is set; fastmcp
constructs the manager at lifespan startup and never forwards the knob, so
``server._enable_mcp_session_reaping`` defaults it on the SDK constructor.
"""
from __future__ import annotations

import pytest
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from fd_open_data_mcp import server


@pytest.fixture
def unpatched():
    """Restore whatever __init__ was in place before each test so the patch
    under test never leaks into the rest of the suite."""
    saved = StreamableHTTPSessionManager.__init__
    yield
    StreamableHTTPSessionManager.__init__ = saved


def _fresh_manager(**kw):
    # The fastmcp subclass is what the real app constructs (during lifespan);
    # it forwards explicit args to the SDK base __init__ the patch wraps.
    from fastmcp.server.http import FastMCPStreamableHTTPSessionManager

    return FastMCPStreamableHTTPSessionManager(app=object(), **kw)


def test_idle_reaping_enabled_by_default(unpatched, monkeypatch):
    monkeypatch.delenv("MCP_SESSION_IDLE_TIMEOUT", raising=False)
    server._enable_mcp_session_reaping()
    assert _fresh_manager().session_idle_timeout == 1800.0


def test_idle_timeout_env_override(unpatched, monkeypatch):
    monkeypatch.setenv("MCP_SESSION_IDLE_TIMEOUT", "600")
    server._enable_mcp_session_reaping()
    assert _fresh_manager().session_idle_timeout == 600.0


def test_zero_disables_reaping(unpatched, monkeypatch):
    monkeypatch.setenv("MCP_SESSION_IDLE_TIMEOUT", "0")
    server._enable_mcp_session_reaping()
    assert _fresh_manager().session_idle_timeout is None


def test_bad_env_falls_back_to_default(unpatched, monkeypatch):
    monkeypatch.setenv("MCP_SESSION_IDLE_TIMEOUT", "not-a-number")
    server._enable_mcp_session_reaping()
    assert _fresh_manager().session_idle_timeout == 1800.0


def test_patch_is_idempotent(unpatched, monkeypatch):
    monkeypatch.delenv("MCP_SESSION_IDLE_TIMEOUT", raising=False)
    server._enable_mcp_session_reaping()
    once = StreamableHTTPSessionManager.__init__
    assert getattr(once, "_fd_reaping_patch", False)
    server._enable_mcp_session_reaping()
    assert StreamableHTTPSessionManager.__init__ is once


def test_explicit_timeout_is_not_overridden(unpatched, monkeypatch):
    # setdefault semantics: a caller that passes the knob explicitly wins.
    # (fastmcp's own constructor cannot forward it — that's why the patch
    # targets the SDK base class.)
    monkeypatch.delenv("MCP_SESSION_IDLE_TIMEOUT", raising=False)
    server._enable_mcp_session_reaping()
    mgr = StreamableHTTPSessionManager(app=object(), session_idle_timeout=300.0)
    assert mgr.session_idle_timeout == 300.0
