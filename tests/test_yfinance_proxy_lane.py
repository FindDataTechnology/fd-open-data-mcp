"""yfinance must egress through the proxy the attempt was given.

Found 2026-11-08: every direct egress in the fleet is blocked by Yahoo (429 from
tencent, 403 from the cheap nodes, every client and impersonation), while the
same endpoint answers 200 through the proxy pool with browser impersonation.
The pipeline publishes the per-attempt proxy in a contextvar and patches
requests/httpx - but yfinance 1.x runs on curl_cffi, which those patches never
touch, so it egressed direct and died. Handing ``Ticker(session=…)`` a proxied
curl_cffi session is not enough either (its own cookie/crumb connections still
fail); only the global config works.
"""
from __future__ import annotations

import os
import sys
import types

import pytest

from fd_open_data_mcp.fetch import runner
from fd_open_data_mcp.proxy import injection

PROXY = "http://user:pass@10.0.0.1:3128"


def _fake_yfinance(calls: list[str]) -> types.ModuleType:
    mod = types.ModuleType("yfinance")
    mod.config = types.SimpleNamespace(network=types.SimpleNamespace(proxy=None))

    def set_config(proxy="__unset__", retries=None):  # noqa: ANN001
        if proxy == "__unset__":
            return
        # Only an applied proxy counts as a call; the adapter also resets with
        # proxy=None on exit (that is the no-leak guarantee, asserted separately).
        if proxy is not None:
            calls.append(proxy)
        mod.config.network.proxy = proxy

    mod.set_config = set_config
    return mod


@pytest.fixture
def clean_env(monkeypatch):
    for k in ("FD_YF_PROXY", "HTTP_PROXY", "HTTPS_PROXY"):
        monkeypatch.delenv(k, raising=False)
    yield


def test_current_proxy_url_roundtrip(clean_env):
    assert injection.current_proxy_url() is None
    with injection.use_proxy(PROXY):
        assert injection.current_proxy_url() == PROXY
    assert injection.current_proxy_url() is None


def test_run_yfinance_applies_attempt_proxy_and_restores(monkeypatch, clean_env):
    calls: list[str] = []
    yf = _fake_yfinance(calls)
    monkeypatch.setitem(sys.modules, "yfinance", yf)
    seen = {}

    def inner(module, command, params):
        seen["cfg"] = module.config.network.proxy
        seen["env"] = os.environ.get("HTTPS_PROXY")
        seen["command"] = command
        return "RESULT"

    monkeypatch.setattr(runner, "_run_yfinance_inner", inner)
    with injection.use_proxy(PROXY):
        assert runner.run_yfinance("ticker_history", {"symbol": "AAPL"}) == "RESULT"

    assert calls == [PROXY]
    assert seen == {"cfg": PROXY, "env": PROXY, "command": "ticker_history"}
    # nothing leaks past the call
    assert yf.config.network.proxy is None
    assert os.environ.get("HTTPS_PROXY") is None
    assert os.environ.get("HTTP_PROXY") is None


def test_env_override_beats_the_attempt_proxy(monkeypatch, clean_env):
    calls: list[str] = []
    monkeypatch.setitem(sys.modules, "yfinance", _fake_yfinance(calls))
    monkeypatch.setenv("FD_YF_PROXY", "http://direct-only:8080")
    monkeypatch.setattr(runner, "_run_yfinance_inner", lambda *a: "R")

    with injection.use_proxy(PROXY):
        runner.run_yfinance("ticker_history", {"symbol": "AAPL"})
    assert calls == ["http://direct-only:8080"]


def test_no_proxy_keeps_direct_behaviour(monkeypatch, clean_env):
    calls: list[str] = []
    yf = _fake_yfinance(calls)
    monkeypatch.setitem(sys.modules, "yfinance", yf)
    monkeypatch.setattr(runner, "_run_yfinance_inner", lambda *a: "R")

    assert runner.run_yfinance("ticker_history", {"symbol": "AAPL"}) == "R"
    assert calls == []
    assert os.environ.get("HTTPS_PROXY") is None
