"""Per-(cluster, function) demotion tests (fix-silent-zero-yield-crawls D5).

Proves demotion is keyed on the FUNCTION (never the source): a blocked
push2* function of akshare demotes while a datacenter.eastmoney.com function
of the SAME source keeps its rank; a single ok row restores it.
"""
from __future__ import annotations

import datetime as dt

from fd_open_data_mcp.fetch.demote import demote_threshold, demoted_functions, reorder_chain
from fd_open_data_mcp.models import FetchLog, Function, Source


def _seed_functions(session) -> tuple[int, int]:
    src = Source(name="akshare", label="akshare")
    session.add(src)
    session.flush()
    blocked = Function(source_id=src.id, command="fund_etf_hist_em")
    healthy = Function(source_id=src.id, command="stock_zcfz_em")
    session.add_all([blocked, healthy])
    session.flush()
    return blocked.id, healthy.id


def _log(session, fn_id, status, cluster_id=7):
    session.add(FetchLog(
        source="akshare", status=status, function_id=fn_id,
        cluster_id=cluster_id, timestamp=dt.datetime.now(dt.timezone.utc),
    ))


def test_consecutive_failures_demote(session):
    blocked_id, healthy_id = _seed_functions(session)
    for _ in range(demote_threshold()):
        _log(session, blocked_id, "error")
    session.commit()
    bad = demoted_functions(session, 7, [blocked_id, healthy_id])
    assert bad == {blocked_id}


def test_single_ok_breaks_the_streak(session):
    blocked_id, _ = _seed_functions(session)
    for _ in range(demote_threshold() - 1):
        _log(session, blocked_id, "error")
    _log(session, blocked_id, "ok")
    session.commit()
    assert demoted_functions(session, 7, [blocked_id]) == set()


def test_other_cluster_failures_do_not_demote(session):
    blocked_id, _ = _seed_functions(session)
    for _ in range(demote_threshold()):
        _log(session, blocked_id, "error", cluster_id=99)
    session.commit()
    assert demoted_functions(session, 7, [blocked_id]) == set()
    assert demoted_functions(session, None, [blocked_id]) == set()  # read() path


def test_reorder_moves_demoted_to_end_preserving_order(session):
    blocked_id, healthy_id = _seed_functions(session)
    for _ in range(demote_threshold()):
        _log(session, blocked_id, "error")
    session.commit()
    chain = [
        {"source": "akshare", "function_id": blocked_id, "command": "a"},
        {"source": "akshare", "function_id": healthy_id, "command": "b"},
        {"source": "wbgapi", "function_id": None, "command": "c"},
    ]
    healthy, demoted = reorder_chain(session, 7, chain)
    assert [c["command"] for c in healthy] == ["b", "c"]
    assert [c["command"] for c in demoted] == ["a"]
