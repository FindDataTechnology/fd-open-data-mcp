"""Integration tests for the proxy/circuit/fetch pipeline (G9).

Exercises the full ``instrumented_fetch`` -> ``classify`` -> ``circuit`` ->
rotate path end-to-end with a stateful in-memory Redis and a fake forwarder
that honors ``circuit.is_selectable`` — so the state machine in ``circuit.py``
is the real one under test, not a mock. Covers Bugs 1+2+3+4+5 together and the
probe path (risk R7). Network-free: ``run_upstream`` is monkeypatched.
"""
from __future__ import annotations

import time
from unittest.mock import patch

import pytest

import fd_open_data_mcp.fetch.instrumentation as instr_mod
import fd_open_data_mcp.proxy.injection as injection
from fd_open_data_mcp.fetch.instrumentation import SourceUnavailable
from fd_open_data_mcp.fetch.runner import FetchError
from fd_open_data_mcp.models import BanRule, FetchLog, Proxy, SourceProbe
from fd_open_data_mcp.proxy import ban_rules, circuit


class _FakeRedis:
    """Minimal in-memory Redis backing the circuit hash + outcomes stream.

    Stateful (unlike a ``MagicMock``) so ``record_outcome`` writes persist and
    are read back by ``get_state`` / ``is_selectable`` across calls — the
    property the integration path depends on. Implements only the callsites
    ``circuit.py`` uses.
    """

    def __init__(self):
        self._h: dict[str, dict[str, str]] = {}
        self.streams: dict[str, list] = {}

    def ping(self):
        return True

    def hgetall(self, key):
        return dict(self._h.get(key, {}))

    def hset(self, key, mapping=None, **kwargs):
        if mapping:
            self._h.setdefault(key, {}).update(
                {k: str(v) for k, v in mapping.items()})
        return 0

    def scan_iter(self, match=None, count=None):
        import fnmatch
        for k in list(self._h.keys()):
            if match is None or fnmatch.fnmatch(k, match):
                yield k

    def xadd(self, name, fields, maxlen=None, approximate=None):
        self.streams.setdefault(name, []).append(fields)
        return b"0-0"


@pytest.fixture
def fake_redis():
    """Patch ``circuit._client`` to return a stateful in-memory Redis."""
    fr = _FakeRedis()
    with patch("fd_open_data_mcp.proxy.circuit._client", return_value=fr):
        yield fr


@pytest.fixture(autouse=True)
def _clear_rule_cache():
    """``ban_rules._CACHE`` is module-level; clear it around each test so a
    fresh-DB test never reads a previous test's cached rule set."""
    ban_rules._CACHE.clear()
    yield
    ban_rules._CACHE.clear()


class _FakeForwarder:
    """Test double for ``injection.proxy_client``.

    Mimics the forwarder's acquire/release using the REAL
    ``circuit.is_selectable`` and ``circuit.record_outcome`` (both backed by
    ``fake_redis``) so the full state machine is exercised: a proxy whose
    circuit tripped OPEN is skipped on the next acquire, and outcomes are
    recorded back to the circuit.
    """

    def __init__(self, proxy_ids):
        self._ids = list(proxy_ids)
        self.returned: list[int] = []

    def acquire(self, source, exclude=None):
        excl = set(exclude or [])
        for pid in self._ids:
            if pid in excl:
                continue  # tried this one already in this fetch (Bug 5)
            if not circuit.is_selectable(source, pid):
                continue  # circuit OPEN/permanent -> rotate (the whole point)
            self.returned.append(pid)
            return injection.Acquisition(
                upstream_url=f"http://p{pid}:30080", addr_id=pid, provider="test")
        return injection._DIRECT_ACQ  # all excluded/open -> direct sentinel

    def release(self, source, addr_id, provider, outcome):
        if addr_id is None:
            return
        circuit.record_outcome(source, addr_id, outcome)


def test_403_opens_circuit_then_rotates_to_next_proxy(
        session, fake_redis, monkeypatch):
    """3 consecutive 403s on eastmoney trip ``circuit:eastmoney:1`` OPEN, then
    the next acquire returns a different proxy (Bugs 1+2+4+5 together).

    Bug 1: eastmoney has no rules -> ``REAL_SOURCE_FALLBACK`` -> akshare 403.
    Bug 2: ``FetchError.status_code`` (403) threaded into ``classify`` (was None).
    Bug 4: ``FetchError`` carries ``status_code``/``response_text``.
    Bug 5: tried proxy excluded so a dead proxy isn't re-acquired.
    """
    session.add(BanRule(source="akshare", rule_type="status", pattern="403",
                        classification="ban", streak_min=0, priority=100))
    session.commit()

    fw = _FakeForwarder([1, 2])
    monkeypatch.setattr("fd_open_data_mcp.proxy.injection.proxy_client", fw)

    def fake_run(source, command, params):
        raise FetchError("403 forbidden", status_code=403,
                         response_text="forbidden")

    monkeypatch.setattr(instr_mod, "run_upstream", fake_run)

    # 3 calls, each max_proxies=1 -> exactly one ban recorded on proxy 1 per
    # call (the inner ban-break ends the call with SourceUnavailable). At
    # BAN_THRESHOLD=3 the circuit trips OPEN.
    for _ in range(3):
        with pytest.raises(SourceUnavailable):
            instr_mod.instrumented_fetch(
                "akshare", "stock_zh_a_hist", {"symbol": "600519"},
                session=session, real_source="eastmoney", max_proxies=1)

    st = circuit.get_state("eastmoney", 1)
    assert st["state"] == "open"
    assert st["fail_streak"] == 3

    # The next acquire rotates to proxy 2 (proxy 1's circuit is OPEN).
    with pytest.raises(SourceUnavailable):
        instr_mod.instrumented_fetch(
            "akshare", "stock_zh_a_hist", {"symbol": "600519"},
            session=session, real_source="eastmoney", max_proxies=1)

    assert fw.returned[:3] == [1, 1, 1]   # first three calls hit proxy 1
    assert fw.returned[3] == 2             # fourth call rotated to proxy 2


def test_transient_timeouts_open_circuit_without_touching_fail_streak(
        session, fake_redis, monkeypatch):
    """Repeated transient outcomes (timeout / RemoteDisconnected — no HTTP
    status) trip the circuit OPEN via the separate ``transient_streak``
    counter with the short ``TRANSIENT_COOLDOWN_SEC``, and NEVER touch
    ``fail_streak`` (Bug 3)."""
    # No ban rules -> classify defaults to transient for a connection error
    # (http_status=None, no rule matches).
    fw = _FakeForwarder([1])
    monkeypatch.setattr("fd_open_data_mcp.proxy.injection.proxy_client", fw)

    def fake_run(source, command, params):
        raise FetchError("RemoteDisconnected('Remote end closed connection')")

    monkeypatch.setattr(instr_mod, "run_upstream", fake_run)

    # Each call does 2 transient attempts (inner range(2): attempt 0 continues,
    # attempt 1 breaks) before the call ends with SourceUnavailable, so 2 calls
    # record 4 transients -> trips OPEN at 3, fail_streak stays 0.
    for _ in range(2):
        with pytest.raises(SourceUnavailable):
            instr_mod.instrumented_fetch(
                "akshare", "stock_zh_a_hist", {"symbol": "600519"},
                session=session, real_source="eastmoney", max_proxies=1)

    st = circuit.get_state("eastmoney", 1)
    assert st["state"] == "open"
    assert st["fail_streak"] == 0            # transient never touches fail_streak
    assert st["transient_streak"] >= circuit.TRANSIENT_THRESHOLD
    # Transient cooldown (120s), not the ban cooldown (600s).
    assert st["cooldown_until"] is not None
    now = time.time()
    remaining = st["cooldown_until"] - now
    assert 0 < remaining <= circuit.TRANSIENT_COOLDOWN_SEC + 10
    assert remaining < circuit.COOLDOWN_SEC


def test_probe_classifies_403_as_ban_not_transient(
        session, fake_redis, monkeypatch):
    """The probe path (``probe/job.py``) threads ``FetchError.status_code`` into
    ``classify`` so a 403 is classified as ban, not transient (Bug 2 / G3 risk
    R7 — the probe path had the same None/None bug as instrumentation)."""
    import fd_open_data_mcp.probe.job as probe_mod

    session.add(BanRule(source="akshare", rule_type="status", pattern="403",
                        classification="ban", streak_min=0, priority=100))
    session.add(SourceProbe(source="eastmoney", command="stock_zh_a_hist",
                            params={"symbol": "600519"}, enabled=True))
    session.add(Proxy(scheme="http", ip="127.0.0.1", port=30080,
                      status="active", label="test"))
    session.commit()
    proxy = session.query(Proxy).first()

    def fake_run(source, command, params):
        raise FetchError("403 forbidden", status_code=403,
                         response_text="forbidden")

    monkeypatch.setattr(probe_mod, "run_upstream", fake_run)

    healthy = probe_mod._probe_one(session, "eastmoney", proxy)
    assert healthy is False  # ban -> not healthy

    log = session.query(FetchLog).filter_by(proxy_id=proxy.id).one()
    assert log.classification == "probe:ban"  # not probe:transient


def test_yahoo_finance_429_classified_ban_via_yfinance_fallback(
        session, fake_redis, monkeypatch):
    """A 429 from ``yahoo_finance`` matches the yfinance 429 rule via the
    ``REAL_SOURCE_FALLBACK`` mapping, through the full ``instrumented_fetch``
    pipeline (Bug 1, the yfinance leg)."""
    session.add(BanRule(source="yfinance", rule_type="status", pattern="429",
                        classification="ban", streak_min=0, priority=100))
    session.commit()

    fw = _FakeForwarder([1])
    monkeypatch.setattr("fd_open_data_mcp.proxy.injection.proxy_client", fw)

    def fake_run(source, command, params):
        raise FetchError("429 too many requests", status_code=429,
                         response_text="too many requests")

    monkeypatch.setattr(instr_mod, "run_upstream", fake_run)

    with pytest.raises(SourceUnavailable):
        instr_mod.instrumented_fetch(
            "yfinance", "ticker_history", {"symbol": "AAPL"},
            session=session, real_source="yahoo_finance", max_proxies=1)

    # The 429 was classified as ban (not transient) -> fail_streak incremented
    # on the yahoo_finance circuit (circuit_source = real_source).
    st = circuit.get_state("yahoo_finance", 1)
    assert st["fail_streak"] == 1
