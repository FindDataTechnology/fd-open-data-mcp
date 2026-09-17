"""Permanent vs transient fetch-failure classification.

A failure intrinsic to the request — the endpoint does not exist in the
installed library — can never succeed on retry. Before this, such a failure
matched no rule, defaulted to ``transient``, and was retried and scored against
a proxy like any network blip: 5,277,961 ``has no callable`` attempts were
logged between 2026-08-30 and 2026-09-16, and the fleet spent 2026-09-16
issuing ~266,000 failed requests in 24 hours with zero successes.

The class is data (a ``ban_rules`` row), not a hardcoded error list.
"""
import pytest

from fd_open_data_mcp.models import BanRule
from fd_open_data_mcp.proxy import ban_rules


@pytest.fixture(autouse=True)
def _clear_rule_cache():
    """The rule cache is module-level and TTL-keyed; clear it around each test
    so a fresh-DB test never reads a previous test's cached rule set."""
    ban_rules._CACHE.clear()
    yield
    ban_rules._CACHE.clear()


def test_unmatched_error_defaults_to_transient(session):
    """Without a matching rule a failed fetch is still transient (no regression)."""
    result = ban_rules.classify(
        session, "akshare", None, "Connection aborted", None, 0)
    assert result == "transient"


def test_explicit_rule_can_classify_permanent(session):
    """A rule row may carry the permanent class, and classify returns it."""
    session.add(BanRule(source="akshare", rule_type="error",
                        pattern="has no callable", classification="permanent",
                        streak_min=0, priority=110))
    session.commit()
    result = ban_rules.classify(
        session, "akshare", None, "akshare has no callable option_czce_hist",
        None, 0)
    assert result == "permanent"


def test_permanent_rule_is_not_gated_by_streak(session):
    """A permanent failure is knowable on the first attempt; a streak gate would
    make the first N failures transient and waste N retries."""
    session.add(BanRule(source="akshare", rule_type="error",
                        pattern="has no callable", classification="permanent",
                        streak_min=0, priority=110))
    session.commit()
    assert ban_rules.classify(
        session, "akshare", None, "akshare has no callable x", None, 0) == "permanent"


def test_unknown_classification_is_ignored(session):
    """A typo'd classification must not reach the retry loop, where an
    unrecognised value would fall through to the re-acquire branch and behave
    like a ban."""
    session.add(BanRule(source="akshare", rule_type="error",
                        pattern="has no callable", classification="permament",
                        streak_min=0, priority=110))
    session.commit()
    result = ban_rules.classify(
        session, "akshare", None, "akshare has no callable x", None, 0)
    assert result == "transient"


def test_classifications_covers_every_handled_value():
    """The documented set matches what the retry loop handles."""
    assert set(ban_rules.CLASSIFICATIONS) == {
        "ok", "transient", "ban", "blocked", "permanent"}


# --- seeded rules (data, not code) -----------------------------------------

def test_seed_installs_permanent_rules_for_unresolvable_endpoints(session):
    """The `has no callable` family is seeded as permanent for every source whose
    runner raises it, so an unresolvable endpoint is recognised as data."""
    from fd_open_data_mcp.proxy.seed import _seed_ban_rules

    _seed_ban_rules(session)
    session.commit()
    ban_rules._CACHE.clear()

    for source in ("akshare", "yfinance", "edgar", "wbgapi"):
        assert ban_rules.classify(
            session, source, None, f"{source} has no callable something", None, 0
        ) == "permanent"


def test_seeded_permanent_rule_does_not_disturb_route_signals(session):
    """A 403/429/timeout keeps its existing class; the permanent rule is matched
    on the error text and must not swallow route signals."""
    from fd_open_data_mcp.proxy.seed import _seed_ban_rules

    _seed_ban_rules(session)
    session.commit()
    ban_rules._CACHE.clear()

    assert ban_rules.classify(session, "akshare", 403, None, None, 0) == "ban"
    assert ban_rules.classify(session, "akshare", 429, None, None, 0) == "ban"
    assert ban_rules.classify(session, "akshare", 500, None, None, 0) == "transient"
    assert ban_rules.classify(
        session, "akshare", None, "read timed out", None, 0) == "transient"
    assert ban_rules.classify(
        session, "akshare", None, "Connection aborted", None, 3) == "ban"


def test_reported_incident_message_classifies_permanent(session):
    """The exact message that produced 5,277,961 attempts classifies permanent."""
    from fd_open_data_mcp.proxy.seed import _seed_ban_rules

    _seed_ban_rules(session)
    session.commit()
    ban_rules._CACHE.clear()

    assert ban_rules.classify(
        session, "akshare", None,
        "akshare has no callable option_czce_hist", None, 0) == "permanent"
    assert ban_rules.classify(
        session, "akshare", None,
        "akshare has no callable stock_zh_a_tick_tx", None, 0) == "permanent"
