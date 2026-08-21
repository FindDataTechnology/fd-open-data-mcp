"""Tests for ban-rule namespace fallback (Bug 1 / G2).

The root cause: ``ban_rules`` are seeded for *library* names (``akshare``) but
``classify`` is called with *real_source* names (``eastmoney``). The
``REAL_SOURCE_FALLBACK`` map lets a real_source with no rules of its own fall
back to its library's rules, while explicit real_source rules always win.
"""
import pytest

from fd_open_data_mcp.models import BanRule
from fd_open_data_mcp.proxy import ban_rules


def _seed_akshare_rules(session):
    """Seed the akshare 403/429 ban rules into the fresh test DB."""
    session.add_all([
        BanRule(source="akshare", rule_type="status", pattern="403",
                classification="ban", streak_min=0, priority=100),
        BanRule(source="akshare", rule_type="status", pattern="429",
                classification="ban", streak_min=0, priority=100),
    ])
    session.commit()


@pytest.fixture(autouse=True)
def _clear_rule_cache():
    """The ban_rules._CACHE is module-level and TTL-keyed; clear it around each
    test so a fresh-DB test never reads a previous test's cached rule set."""
    ban_rules._CACHE.clear()
    yield
    ban_rules._CACHE.clear()


def test_classify_eastmoney_falls_back_to_akshare_rules(session):
    """eastmoney has no direct rules -> fallback to akshare 403 rule -> ban."""
    _seed_akshare_rules(session)
    result = ban_rules.classify(session, "eastmoney", 403, None, None, 0)
    assert result == "ban"


def test_classify_akshare_direct_matches_without_fallback(session):
    """A library name loads its rules directly; no fallback invoked."""
    _seed_akshare_rules(session)
    result = ban_rules.classify(session, "akshare", 403, None, None, 0)
    assert result == "ban"


def test_classify_unknown_real_source_defaults_transient(session):
    """A real_source with no rules and no fallback mapping -> default transient
    for a non-2xx status (no regression vs. pre-fallback behavior)."""
    result = ban_rules.classify(session, "new_real_source", 403, None, None, 0)
    assert result == "transient"


def test_explicit_real_source_rule_wins_over_fallback(session):
    """If a real_source has its OWN 403 rule, it takes precedence over the
    library fallback (fallback only fires when the primary lookup is empty)."""
    session.add_all([
        BanRule(source="akshare", rule_type="status", pattern="403",
                classification="ban", streak_min=0, priority=100),
        BanRule(source="eastmoney", rule_type="status", pattern="403",
                classification="blocked", streak_min=0, priority=100),
    ])
    session.commit()
    result = ban_rules.classify(session, "eastmoney", 403, None, None, 0)
    assert result == "blocked"


def test_yahoo_finance_falls_back_to_yfinance(session):
    """yahoo_finance -> yfinance fallback (the yfinance library mapping)."""
    session.add(BanRule(source="yfinance", rule_type="status", pattern="429",
                        classification="ban", streak_min=0, priority=100))
    session.commit()
    result = ban_rules.classify(session, "yahoo_finance", 429, None, None, 0)
    assert result == "ban"


def test_combined_streak_gates_streak_min_rule(session):
    """A streak_min-gated rule (RemoteDisconnected -> ban, streak>=3) fires when
    the combined streak reaches 3 via transient outcomes alone — the read-only
    combined streak passed by instrumentation/probe (Bug 3 + Bug 1 together)."""
    session.add(BanRule(source="akshare", rule_type="error",
                        pattern="RemoteDisconnected", classification="ban",
                        streak_min=3, priority=80))
    session.commit()
    # streak below gate -> default transient (403 is non-2xx, no rule matches)
    assert ban_rules.classify(
        session, "eastmoney", None, "RemoteDisconnected", None, 2) == "transient"
    # streak at gate -> ban rule matches
    assert ban_rules.classify(
        session, "eastmoney", None, "RemoteDisconnected", None, 3) == "ban"
