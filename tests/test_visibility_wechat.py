"""Tests for the ServerChan notifier sink (add-crawl-visibility)."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from fd_open_data_mcp.visibility.notifiers import wechat


def test_send_posts_to_sctapi(monkeypatch):
    """A valid token POSTs title/des form to sctapi.ftqq.com/<token>.send."""
    captured = {}

    class _Resp:
        def read(self):
            return json.dumps({"code": 0}).encode()

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["data"] = req.data.decode()
        captured["method"] = req.get_method()
        return _Resp()

    monkeypatch.setattr(wechat.urllib.request, "urlopen", fake_urlopen)
    n = wechat.ServerChanNotifier(token="TESTTOKEN")
    n.send("🚨 CRAWL FAILURE", "body line 1\nbody line 2")

    assert captured["url"] == "https://sctapi.ftqq.com/TESTTOKEN.send"
    assert captured["method"] == "POST"
    assert "title=%F0%9F%9A%A8+CRAWL+FAILURE" in captured["data"] or "title=" in captured["data"]
    assert "des=body+line+1" in captured["data"]


def test_send_truncates_title_and_body(monkeypatch):
    """title is capped at 32 chars and body at 32KB."""
    captured = {}

    class _Resp:
        def read(self):
            return json.dumps({"code": 0}).encode()

    def fake_urlopen(req, timeout=None):
        captured["data"] = req.data.decode()
        return _Resp()

    monkeypatch.setattr(wechat.urllib.request, "urlopen", fake_urlopen)
    n = wechat.ServerChanNotifier(token="T")
    long_title = "X" * 200
    long_body = "Y" * 50_000
    n.send(long_title, long_body)

    # urllib.parse.urlencode puts title=...&des=...
    parts = dict(p.split("=", 1) for p in captured["data"].split("&"))
    assert len(parts["title"]) <= 32  # truncated before encoding
    assert len(parts["des"]) <= 32_000


def test_missing_token_does_not_crash(caplog):
    """No token → log + no-op, never raise (the watcher must stay healthy)."""
    n = wechat.ServerChanNotifier(token="")
    # should not raise
    n.send("title", "body")


def test_transport_error_does_not_crash(monkeypatch):
    """A network error logs and no-ops rather than propagating."""
    def boom(req, timeout=None):
        raise OSError("network down")

    monkeypatch.setattr(wechat.urllib.request, "urlopen", boom)
    n = wechat.ServerChanNotifier(token="T")
    # should not raise
    n.send("title", "body")


def test_null_notifier_logs():
    """NullNotifier.send logs the message (dry-run / unknown-sink fallback)."""
    n = wechat.NullNotifier()
    n.send("title", "body")  # should not raise
