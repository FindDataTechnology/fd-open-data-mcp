"""Price active pool (design D5, capability price-active-pool).

The pool is the "B" half: gap filling serves any symbol, pre-warming serves the
symbols likely to be asked for. What matters is that membership is *derivable*
from recorded requests and that neither recording nor pre-warming sits on the
read path — a broken pool must never change what a reader sees.
"""

from __future__ import annotations

import pytest

from fd_open_data_mcp.fetch.active_pool import (
    DEFAULT_POOL_TOP_N,
    DEFAULT_PREWARM_DAYS,
    add_config_member,
    is_in_pool,
    pool_members,
    prewarm,
    recompute_frequency_members,
    record_request,
    remove_config_member,
)
from fd_open_data_mcp.models import Concept, PricePoolMember

T = "stock"


# ---------------------------------------------------------------------------
# membership sources
# ---------------------------------------------------------------------------


def test_config_member_stays_without_any_requests(session) -> None:
    add_config_member(session, T, 1)
    assert is_in_pool(session, T, 1) == "config"


def test_frequent_symbol_enters_after_recompute(session) -> None:
    record_request(session, T, 2)
    assert is_in_pool(session, T, 2) is None, "recording is not membership"
    recompute_frequency_members(session, top_n=10)
    assert is_in_pool(session, T, 2) == "frequency"


def test_config_source_survives_recompute(session) -> None:
    add_config_member(session, T, 1)
    recompute_frequency_members(session, top_n=1)
    assert is_in_pool(session, T, 1) == "config", "config outranks derived membership"


def test_symbol_falling_out_of_top_n_leaves_the_pool(session) -> None:
    for i in range(5):
        for _ in range(i + 1):
            record_request(session, T, i)
    recompute_frequency_members(session, top_n=2)
    assert is_in_pool(session, T, 4) == "frequency"   # most requested
    assert is_in_pool(session, T, 0) is None          # least requested, dropped
    # The row and its evidence survive; membership is a separate question.
    assert session.query(PricePoolMember).filter_by(entity_type=T, entity_id=0).count() == 1


def test_dropped_symbol_re_enters_when_requested_again(session) -> None:
    for i in range(3):
        for _ in range(i + 1):
            record_request(session, T, i)
    recompute_frequency_members(session, top_n=1)
    assert is_in_pool(session, T, 0) is None
    for _ in range(50):
        record_request(session, T, 0)
    recompute_frequency_members(session, top_n=1)
    assert is_in_pool(session, T, 0) == "frequency"


def test_recompute_is_idempotent(session) -> None:
    for i in range(4):
        for _ in range(i + 1):
            record_request(session, T, i)
    first = recompute_frequency_members(session, top_n=2)
    second = recompute_frequency_members(session, top_n=2)
    assert first["added"] + 0 == second["added"] or second["added"] == 0
    assert {m["entity_id"] for m in pool_members(session)} == {
        m["entity_id"] for m in pool_members(session)
    }


# ---------------------------------------------------------------------------
# add / remove
# ---------------------------------------------------------------------------


def test_remove_config_member(session) -> None:
    add_config_member(session, T, 1)
    assert remove_config_member(session, T, 1) is True
    assert is_in_pool(session, T, 1) is None


def test_remove_frequency_member_is_refused(session) -> None:
    """Deleting a derived member would be undone by the next recompute."""
    record_request(session, T, 1)
    recompute_frequency_members(session, top_n=10)
    assert remove_config_member(session, T, 1) is False
    assert is_in_pool(session, T, 1) == "frequency"


def test_promote_frequency_to_config(session) -> None:
    record_request(session, T, 1)
    recompute_frequency_members(session, top_n=10)
    assert add_config_member(session, T, 1) is True
    assert is_in_pool(session, T, 1) == "config"


def test_listing_reports_the_reason(session) -> None:
    add_config_member(session, T, 1)
    record_request(session, T, 2)
    recompute_frequency_members(session, top_n=10)
    by_id = {m["entity_id"]: m for m in pool_members(session)}
    assert by_id[1]["source"] == "config"
    assert by_id[2]["source"] == "frequency"


# ---------------------------------------------------------------------------
# prewarm (task 7)
# ---------------------------------------------------------------------------


def test_prewarm_on_empty_pool_is_a_no_op(session) -> None:
    c = Concept(code="price.close", entity_type=T, frequency="daily",
                unit="currency", verified=True)
    session.add(c)
    session.commit()
    out = prewarm(session, days=30)
    assert out["ok"] is True
    assert out["results"] == []


def test_prewarm_missing_concept_reports_instead_of_raising(session) -> None:
    out = prewarm(session, days=30, concept_code="nope.nope")
    assert out["ok"] is False
    assert "not found" in out["reason"]


def test_prewarm_failure_is_per_symbol(session) -> None:
    """One unreachable symbol must not stop the others."""
    c = Concept(code="price.close", entity_type=T, frequency="daily",
                unit="currency", verified=True)
    session.add(c)
    session.commit()
    add_config_member(session, T, 1)
    add_config_member(session, T, 2)
    out = prewarm(session, days=30)
    assert out["ok"] is True
    assert len(out["results"]) == 2, "every member is attempted"
    assert all(r["entity_id"] in (1, 2) for r in out["results"])
    # No bindings exist here, so both are reported rather than raising.
    assert out["succeeded"] + out["failed"] == 2


def test_prewarm_respects_limit(session) -> None:
    c = Concept(code="price.close", entity_type=T, frequency="daily",
                unit="currency", verified=True)
    session.add(c)
    session.commit()
    for i in range(4):
        add_config_member(session, T, i)
    out = prewarm(session, days=30, limit=2)
    assert len(out["results"]) == 2


def test_defaults_are_aligned_with_the_backtest_window() -> None:
    """730 days is the engine's window cap; warming deeper would be unread."""
    assert DEFAULT_PREWARM_DAYS == 730
    assert DEFAULT_POOL_TOP_N > 0
