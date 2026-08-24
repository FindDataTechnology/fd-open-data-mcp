"""Tests for the Redis dedup-state helpers (add-crawl-visibility)."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from fd_open_data_mcp.visibility import state


@pytest.fixture
def mock_redis():
    """Patch the state module's lazy redis client with a MagicMock."""
    with patch("fd_open_data_mcp.visibility.state._client") as mock_client:
        r = MagicMock()
        mock_client.return_value = r
        yield r


def test_no_redis_watermark_is_none(monkeypatch):
    """Without Redis, get_scan_watermark returns None (dark mode)."""
    monkeypatch.delenv("REDIS_URL", raising=False)
    state._REDIS = None
    assert state.get_scan_watermark() is None


def test_watermark_roundtrip(mock_redis):
    """set_scan_watermark writes; get_scan_watermark reads it back as float."""
    mock_redis.get.return_value = "1700000000.0"
    assert state.get_scan_watermark() == 1700000000.0
    state.set_scan_watermark(1700000050.0)
    mock_redis.set.assert_called_once_with(
        "crawl_watcher:scan:last_ts", "1700000050.0")


def test_watermark_only_advances(mock_redis):
    """set_scan_watermark with an older ts does not regress the watermark."""
    mock_redis.get.return_value = "1700000100.0"  # current watermark newer
    state.set_scan_watermark(1700000050.0)  # older
    mock_redis.set.assert_not_called()


def test_already_alerted_false_then_true(mock_redis):
    """already_alerted returns False then True after mark_alerted."""
    mock_redis.exists.return_value = 0
    assert state.already_alerted(42, "failed") is False
    mock_redis.exists.return_value = 1
    assert state.already_alerted(42, "failed") is True


def test_mark_alerted_sets_ttl(mock_redis):
    """mark_alerted writes the key with a 7-day TTL."""
    state.mark_alerted(42, "stale")
    args, kwargs = mock_redis.set.call_args
    assert args[0] == "crawl_watcher:alerted:42:stale"
    assert args[1] == "1"
    assert kwargs["ex"] == 7 * 24 * 3600


def test_already_alerted_false_without_redis(monkeypatch):
    """Without Redis, already_alerted is False (best-effort: alert at least once)."""
    monkeypatch.delenv("REDIS_URL", raising=False)
    state._REDIS = None
    assert state.already_alerted(1, "failed") is False
