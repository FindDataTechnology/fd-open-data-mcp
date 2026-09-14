"""Grouped-circuit behaviour, crawler side.

The crawler never *writes* circuit state (the forwarder owns the state machine)
but it reads the same keys: the probe job recovers OPEN circuits and the cluster
scheduler consults them at dispatch time. Both must key by the circuit unit, or
a grouped OPEN is invisible to the crawler while the forwarder treats the whole
exit as dead.
"""
from __future__ import annotations

import fnmatch
from unittest.mock import patch

import pytest

from fd_open_data_mcp.models import Proxy
from fd_open_data_mcp.proxy import circuit, pool

EXIT_A = "38.76.150.159"


class _FakeRedis:
    """Hash-only in-memory Redis — all this side touches is circuit state."""

    def __init__(self):
        self._h: dict[str, dict[str, str]] = {}

    def ping(self):
        return True

    def hgetall(self, key):
        return dict(self._h.get(key, {}))

    def hset(self, key, mapping=None, **kwargs):
        if mapping:
            self._h.setdefault(key, {}).update(
                {k: str(v) for k, v in mapping.items()})
        return 0

    def hdel(self, key, *fields):
        h = self._h.get(key, {})
        n = 0
        for f in fields:
            if f in h:
                del h[f]
                n += 1
        return n

    def scan_iter(self, match=None, count=None):
        for k in list(self._h.keys()):
            if match is None or fnmatch.fnmatch(k, match):
                yield k

    def xadd(self, name, fields, maxlen=None, approximate=None):
        return b"0-0"


@pytest.fixture
def fake_redis():
    """Patch this package's circuit client with the stateful fake."""
    fr = _FakeRedis()
    with patch("fd_open_data_mcp.proxy.circuit._client", return_value=fr):
        yield fr


def _addr(session, port, exit_ip=None):
    row = Proxy(
        scheme="http", ip="100.64.0.7", port=port, status="active",
        label=f"mihomo-{port}", provider="mihomo",
        provider_meta=({"exit_ip": exit_ip} if exit_ip else None),
    )
    session.add(row)
    session.commit()
    return row


# ─── unit format (must match fd-proxy-service's, they share the key space) ──

def test_unit_reuses_the_legacy_key_when_no_exit_ip(session):
    a = _addr(session, 30080)
    assert pool.circuit_unit(a) == str(a.id)
    assert f"circuit:eastmoney:{pool.circuit_unit(a)}" == f"circuit:eastmoney:{a.id}"


def test_unit_is_the_shared_exit_ip(session):
    a = _addr(session, 30081, EXIT_A)
    assert pool.circuit_unit(a) == EXIT_A


# ─── probe scan tolerates a non-numeric unit ────────────────────────────────

def test_open_for_probe_returns_a_non_numeric_unit(session, fake_redis):
    """The scan used to ``int(parts[2])`` and raised ValueError on an exit IP,
    which killed the whole recovery loop."""
    _addr(session, 30081, EXIT_A)
    fake_redis.hset(f"circuit:fred:{EXIT_A}", mapping={
        "state": "open", "cooldown_until": "1", "permanent": "0"})

    assert ("fred", EXIT_A) in circuit.open_for_probe()


# ─── resolving a unit to a probe target ─────────────────────────────────────

def test_proxy_for_unit_picks_the_lowest_active_member(session):
    a = _addr(session, 30081, EXIT_A)
    b = _addr(session, 30083, EXIT_A)
    lo, hi = sorted((a, b), key=lambda r: r.id)

    assert pool.proxy_for_unit(session, EXIT_A).id == lo.id

    lo.status = "retired"
    session.commit()
    assert pool.proxy_for_unit(session, EXIT_A).id == hi.id

    hi.status = "retired"
    session.commit()
    assert pool.proxy_for_unit(session, EXIT_A) is None


def test_proxy_for_unit_resolves_a_plain_id_unit(session):
    a = _addr(session, 30080)
    assert pool.proxy_for_unit(session, str(a.id)).id == a.id


# ─── group recovery ────────────────────────────────────────────────────────

def test_probe_recovers_the_whole_group(session, fake_redis):
    """One successful probe closes the exit for every port behind it."""
    a = _addr(session, 30081, EXIT_A)
    b = _addr(session, 30083, EXIT_A)
    for _ in range(circuit.BAN_THRESHOLD):
        circuit.record_outcome("fred", EXIT_A, "ban")
    assert circuit.is_selectable("fred", EXIT_A) is False

    circuit.probe_transition("fred", EXIT_A, probe_ok=True)

    for row in (a, b):
        assert circuit.is_selectable("fred", pool.circuit_unit(row)) is True
