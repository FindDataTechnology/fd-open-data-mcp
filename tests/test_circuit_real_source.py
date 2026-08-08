"""Unit tests for proxy circuit breaker with real_source support.

Tests verify that circuit breaker correctly tracks ban state at the real data source
level (e.g., "eastmoney", "tencent") instead of library level (e.g., "akshare").
"""
import os
import pytest
from unittest.mock import MagicMock, patch
from fd_open_data_mcp.proxy import circuit


@pytest.fixture
def mock_redis():
    """Mock Redis client for testing."""
    with patch('fd_open_data_mcp.proxy.circuit._client') as mock_client:
        mock_r = MagicMock()
        mock_client.return_value = mock_r
        yield mock_r


def test_key_generation_with_real_source():
    """Test that circuit key is generated correctly for real_source."""
    key = circuit._key("eastmoney", 1)
    assert key == "circuit:eastmoney:1"


def test_key_generation_with_library_name():
    """Test that circuit key works with library name (backward compatibility)."""
    key = circuit._key("akshare", 1)
    assert key == "circuit:akshare:1"


def test_get_state_default_closed(mock_redis):
    """Test that get_state returns CLOSED by default when no circuit exists."""
    mock_redis.hgetall.return_value = {}
    state = circuit.get_state("eastmoney", 1)
    assert state["state"] == "closed"
    assert state["fail_streak"] == 0
    assert state["permanent"] is False


def test_record_outcome_ban_increments_fail_streak(mock_redis):
    """Test that recording a BAN outcome increments fail_streak."""
    # Setup: circuit starts closed with fail_streak=0
    mock_redis.hgetall.return_value = {
        "state": "closed",
        "fail_streak": "0",
        "success_streak": "0",
        "open_cycles": "0",
        "permanent": "0"
    }

    # Record a ban
    result = circuit.record_outcome("eastmoney", 1, "ban")

    # Verify fail_streak incremented
    assert result["fail_streak"] == 1
    assert result["state"] == "closed"  # Not yet at threshold


def test_record_outcome_ban_trips_circuit_at_threshold(mock_redis):
    """Test that reaching BAN_THRESHOLD trips the circuit to OPEN."""
    # Setup: circuit at threshold-1
    mock_redis.hgetall.return_value = {
        "state": "closed",
        "fail_streak": "2",  # One below threshold
        "success_streak": "0",
        "open_cycles": "0",
        "permanent": "0"
    }

    # Record another ban (should trip to OPEN)
    result = circuit.record_outcome("eastmoney", 1, "ban")

    # Verify circuit tripped to OPEN
    assert result["state"] == "open"
    assert result["fail_streak"] == 3
    assert result["cooldown_until"] is not None


def test_record_outcome_ok_resets_fail_streak(mock_redis):
    """Test that recording an OK outcome resets fail_streak."""
    # Setup: circuit with some failures
    mock_redis.hgetall.return_value = {
        "state": "closed",
        "fail_streak": "2",
        "success_streak": "5",
        "open_cycles": "0",
        "permanent": "0"
    }

    # Record success
    result = circuit.record_outcome("eastmoney", 1, "ok")

    # Verify fail_streak reset
    assert result["fail_streak"] == 0
    assert result["success_streak"] == 6


def test_is_selectable_closed_circuit(mock_redis):
    """Test that CLOSED circuit is selectable."""
    mock_redis.hgetall.return_value = {
        "state": "closed",
        "fail_streak": "0",
        "permanent": "0"
    }
    assert circuit.is_selectable("eastmoney", 1) is True


def test_is_selectable_open_circuit(mock_redis):
    """Test that OPEN circuit is not selectable."""
    mock_redis.hgetall.return_value = {
        "state": "open",
        "fail_streak": "3",
        "permanent": "0"
    }
    assert circuit.is_selectable("eastmoney", 1) is False


def test_is_selectable_permanent_circuit(mock_redis):
    """Test that PERMANENT circuit is never selectable."""
    mock_redis.hgetall.return_value = {
        "state": "closed",
        "fail_streak": "0",
        "permanent": "1"
    }
    assert circuit.is_selectable("eastmoney", 1) is False


def test_probe_transition_success_closes_circuit(mock_redis):
    """Test that successful probe closes HALF_OPEN circuit."""
    mock_redis.hgetall.return_value = {
        "state": "half_open",
        "fail_streak": "3",
        "open_cycles": "1",
        "cooldown_until": "1234567890.0",
        "permanent": "0"
    }

    result = circuit.probe_transition("eastmoney", 1, probe_ok=True)

    assert result["state"] == "closed"
    assert result["fail_streak"] == 0
    assert result["open_cycles"] == 0


def test_probe_transition_failure_reopens_circuit(mock_redis):
    """Test that failed probe re-opens circuit with doubled cooldown."""
    mock_redis.hgetall.return_value = {
        "state": "half_open",
        "fail_streak": "3",
        "open_cycles": "1",
        "cooldown_until": "1234567890.0",
        "permanent": "0"
    }

    result = circuit.probe_transition("eastmoney", 1, probe_ok=False)

    assert result["state"] == "open"
    assert result["open_cycles"] == 2


def test_probe_transition_permanent_after_max_cycles(mock_redis):
    """Test that circuit becomes PERMANENT after PERMANENT_CYCLES failures."""
    mock_redis.hgetall.return_value = {
        "state": "half_open",
        "fail_streak": "3",
        "open_cycles": "2",  # One below PERMANENT_CYCLES (3)
        "cooldown_until": "1234567890.0",
        "permanent": "0"
    }

    result = circuit.probe_transition("eastmoney", 1, probe_ok=False)

    assert result["state"] == "open"
    assert result["open_cycles"] == 3
    assert result["permanent"] is True


def test_all_circuits_returns_all_sources(mock_redis):
    """Test that all_circuits returns circuits for all sources."""
    mock_redis.scan_iter.return_value = [
        "circuit:eastmoney:1",
        "circuit:tencent:2",
        "circuit:akshare:3"
    ]
    mock_redis.hgetall.side_effect = [
        {"state": "closed", "fail_streak": "0", "open_cycles": "0", "permanent": "0"},
        {"state": "open", "fail_streak": "3", "open_cycles": "1", "permanent": "0"},
        {"state": "closed", "fail_streak": "0", "open_cycles": "0", "permanent": "0"}
    ]

    circuits = circuit.all_circuits()

    assert len(circuits) == 3
    assert circuits[0]["source"] == "eastmoney"
    assert circuits[1]["source"] == "tencent"
    assert circuits[2]["source"] == "akshare"


def test_sources_all_proxies_open(mock_redis):
    """Test sources_all_proxies_open detects when all proxies for a source are OPEN."""
    # Mock all_circuits to return circuits where eastmoney has all proxies OPEN
    with patch('fd_open_data_mcp.proxy.circuit.all_circuits') as mock_all:
        mock_all.return_value = [
            {"source": "eastmoney", "proxy_id": 1, "state": "open", "permanent": False},
            {"source": "eastmoney", "proxy_id": 2, "state": "open", "permanent": False},
            {"source": "tencent", "proxy_id": 1, "state": "closed", "permanent": False}
        ]

        result = circuit.sources_all_proxies_open(None)

        assert "eastmoney" in result
        assert "tencent" not in result
