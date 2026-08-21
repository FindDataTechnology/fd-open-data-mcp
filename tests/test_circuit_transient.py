"""Unit tests for the circuit breaker's transient-streak counter (Bug 3).

Verifies that repeated TRANSIENT outcomes (timeouts, connection resets) trip the
circuit OPEN via a separate ``transient_streak`` — independent of the hard-ban
``fail_streak`` — so a persistently-flaky endpoint is not retried forever.
Mirrors the style of ``test_circuit_real_source.py`` (mock-redis fixture).
"""
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


# The "closed + all-zero" baseline used by most tests.
_CLOSED = {
    "state": "closed", "fail_streak": "0", "transient_streak": "0",
    "success_streak": "0", "open_cycles": "0", "permanent": "0",
}


def test_record_outcome_transient_increments_transient_streak(mock_redis):
    """A transient outcome increments transient_streak, not fail_streak."""
    mock_redis.hgetall.return_value = dict(_CLOSED)
    result = circuit.record_outcome("eastmoney", 1, "transient")
    assert result["transient_streak"] == 1
    assert result["state"] == "closed"  # below TRANSIENT_THRESHOLD
    assert result["fail_streak"] == 0   # transient must NOT touch fail_streak


def test_record_outcome_transient_below_threshold_stays_closed(mock_redis):
    """Two transients (below threshold of 3) keep the circuit closed."""
    state = dict(_CLOSED, transient_streak="1")
    mock_redis.hgetall.return_value = state
    result = circuit.record_outcome("eastmoney", 1, "transient")
    assert result["transient_streak"] == 2
    assert result["state"] == "closed"


def test_record_outcome_transient_trips_open_at_threshold(mock_redis):
    """At TRANSIENT_THRESHOLD consecutive transients the circuit trips OPEN."""
    state = dict(_CLOSED, transient_streak="2")  # one below threshold
    mock_redis.hgetall.return_value = state
    result = circuit.record_outcome("eastmoney", 1, "transient")
    assert result["state"] == "open"
    assert result["transient_streak"] == 3
    assert result["fail_streak"] == 0          # still untouched
    assert result["cooldown_until"] is not None  # tripped -> cooldown set


def test_record_outcome_ok_resets_transient_streak(mock_redis):
    """An ok outcome resets transient_streak (and increments success_streak)."""
    state = dict(_CLOSED, transient_streak="2")
    mock_redis.hgetall.return_value = state
    result = circuit.record_outcome("eastmoney", 1, "ok")
    assert result["transient_streak"] == 0
    assert result["fail_streak"] == 0
    assert result["success_streak"] == 1


def test_record_outcome_ban_resets_transient_streak(mock_redis):
    """A ban (stronger signal) resets transient_streak; it increments fail_streak."""
    state = dict(_CLOSED, transient_streak="2")
    mock_redis.hgetall.return_value = state
    result = circuit.record_outcome("eastmoney", 1, "ban")
    assert result["transient_streak"] == 0
    assert result["fail_streak"] == 1   # ban increments fail_streak, not transient
    assert result["state"] == "closed"   # fail_streak 1 < BAN_THRESHOLD 3


def test_transient_streak_independent_of_fail_streak(mock_redis):
    """Two transients then a ban: fail_streak is 1 (not 3), state stays closed."""
    state = dict(_CLOSED, transient_streak="2")
    mock_redis.hgetall.return_value = state
    result = circuit.record_outcome("eastmoney", 1, "ban")
    # ban path: fail_streak = 0 + 1 = 1; transient_streak reset to 0.
    assert result["fail_streak"] == 1
    assert result["transient_streak"] == 0
    assert result["state"] == "closed"


def test_probe_transition_success_resets_transient_streak(mock_redis):
    """A successful probe closes the circuit and resets transient_streak."""
    state = {
        "state": "half_open", "fail_streak": "0", "transient_streak": "2",
        "open_cycles": "1", "cooldown_until": "1234567890.0", "permanent": "0",
    }
    mock_redis.hgetall.return_value = state
    result = circuit.probe_transition("eastmoney", 1, probe_ok=True)
    assert result["state"] == "closed"
    assert result["transient_streak"] == 0
    assert result["open_cycles"] == 0


def test_probe_transition_failure_resets_transient_streak(mock_redis):
    """A failed probe resets transient_streak so the first transient after
    cooldown does not instantly re-trip the circuit."""
    state = {
        "state": "half_open", "fail_streak": "0", "transient_streak": "2",
        "open_cycles": "1", "cooldown_until": "1234567890.0", "permanent": "0",
    }
    mock_redis.hgetall.return_value = state
    result = circuit.probe_transition("eastmoney", 1, probe_ok=False)
    assert result["state"] == "open"
    assert result["transient_streak"] == 0
    assert result["open_cycles"] == 2
