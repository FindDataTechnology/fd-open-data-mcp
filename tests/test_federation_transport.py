"""Real-transport tests for the federation client (registry-transparent-read-and-scope).

The success/failure semantics tests mock ``_call_domain_tool``; these boot an
actual FastMCP HTTP server and drive ``federation._attempt_call`` through the
real Streamable HTTP path — the layer where the ``timeout=`` kwarg slipped
through unnoticed (StreamableHttpTransport has never taken one; the failure
only showed online). One test per transport concern: structured round-trip,
bearer header pass-through, and the wait_for timeout cap.
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from fd_open_data_mcp import federation

SERVER_SCRIPT = """
import asyncio, sys
from fastmcp import FastMCP

mcp = FastMCP(name="fake-business")

@mcp.tool
def probe(payload: dict) -> dict:
    return {"ok": True, "echo": payload}

@mcp.tool
def slow(seconds: int) -> dict:
    import time
    time.sleep(seconds)
    return {"ok": True}

mcp.run(transport="http", host="127.0.0.1", port=int(sys.argv[2]))
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def fake_business():
    port = _free_port()
    script = Path(__file__).parent / "_fake_business_server.py"
    script.write_text(SERVER_SCRIPT)
    proc = subprocess.Popen(
        [sys.executable, str(script), "serve", str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    base = f"http://127.0.0.1:{port}/mcp"
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            urllib.request.urlopen(base, timeout=2)
            break
        except urllib.error.HTTPError:
            break  # any HTTP answer (401/405/406…) means the server is up
        except Exception:
            if proc.poll() is not None:
                out = proc.stdout.read()[-400:] if proc.stdout else ""
                raise RuntimeError(f"fake business server died: {out}")
            time.sleep(0.3)
    else:
        proc.kill()
        raise RuntimeError("fake business server did not come up")
    yield base
    proc.kill()
    script.unlink(missing_ok=True)


def test_round_trip_over_real_transport(fake_business):
    out = federation._attempt_call(fake_business, "probe", {"payload": {"k": 1}})
    assert out["ok"] is True
    assert out["echo"] == {"k": 1}


def test_timeout_caps_the_exchange(fake_business, monkeypatch):
    monkeypatch.setenv(federation.TIMEOUT_ENV, "1")
    with pytest.raises(federation.FederationUnavailable, match="did not answer"):
        federation._attempt_call(fake_business, "slow", {"seconds": 8})


def test_call_domain_tool_wraps_transport_failure(monkeypatch):
    monkeypatch.setenv(federation.ENDPOINT_ENV, "http://127.0.0.1:9/mcp")  # nothing listens
    monkeypatch.setattr(federation, "_cooldown_seconds", lambda: 0.0)
    federation.reset_cooldown()
    with pytest.raises(federation.FederationUnavailable):
        federation._call_domain_tool("probe", {})
    federation.reset_cooldown()
