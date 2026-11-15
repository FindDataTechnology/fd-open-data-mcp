"""Missed-run board tests (panel-ops-console, spec crawl-control-center).

Covers: a silently-skipped nightly policy is flagged with fire time + reason;
the flag clears after a successful run or disabling; grace interval and the
single-flight / plan-refused reason heuristics.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from fd_open_data_mcp.models import CrawlPolicy, PolicyRun
from fd_open_data_mcp.visibility.snapshot import missed_runs

NOW = datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc)


def _client():
    from fd_open_data_mcp.panel.app import app
    return TestClient(app)


def _policy(session, **over) -> CrawlPolicy:
    kw = dict(name="nightly", enabled=True, concept_ids=[1], entity_type="stock",
              date_policy={"mode": "trailing", "days": 1}, frequency="daily",
              mode="per_date", cron_expr="0 2 * * *", timezone="UTC",
              last_run_at=NOW - timedelta(days=3),  # last ran 3 days ago
              created_at=NOW - timedelta(days=30))
    kw.update(over)
    p = CrawlPolicy(**kw)
    session.add(p)
    session.commit()
    return p


def test_skipped_nightly_policy_is_flagged(session):
    _policy(session)  # 02:00 nightly cron, last run 3 days ago
    flags = missed_runs(session, now=NOW)
    assert len(flags) == 1
    f = flags[0]
    assert f["policy"] == "nightly"
    assert f["reason"] == "never launched"
    assert f["minutes_late"] > 30
    # the flagged fire is the FIRST uncovered fire: 02:00 the day after the
    # last run (2025-06-12T12:00Z) — 2025-06-13T02:00Z
    assert f["missed_fire"].startswith("2025-06-13T02:00")


def test_flag_clears_after_successful_run(session):
    p = _policy(session)
    session.add(PolicyRun(policy_id=p.id, status="success",
                          started_at=NOW - timedelta(hours=1)))
    session.commit()
    assert missed_runs(session, now=NOW) == []


def test_flag_clears_when_disabled(session):
    _policy(session, enabled=False)
    assert missed_runs(session, now=NOW) == []


def test_reason_plan_refused_keeps_flag(session):
    p = _policy(session)
    # yesterday's fire launched and was refused: run recorded, flag stays
    session.add(PolicyRun(policy_id=p.id, status="failed",
                          started_at=NOW - timedelta(days=1),
                          detail="refused: estimated 99999 fetches exceeds "
                                 "POLICY_MAX_FETCHES=50000"))
    session.commit()
    flags = missed_runs(session, now=NOW)
    assert len(flags) == 1 and flags[0]["reason"] == "plan refused"


def test_reason_blocked_by_single_flight(session):
    p = _policy(session)
    # an OPEN run started before the latest fire: the reconciler keeps skipping
    session.add(PolicyRun(policy_id=p.id, status="running",
                          started_at=NOW - timedelta(days=3)))
    session.commit()
    flags = missed_runs(session, now=NOW)
    assert len(flags) == 1 and flags[0]["reason"] == "blocked by single-flight"


def test_grace_window_hides_recent_fire(session):
    # last run covered through yesterday; the first uncovered fire (today
    # 02:00) is only 10h stale — inside the nightly grace (2×24h), so quiet
    p = _policy(session, cron_expr="0 2 * * *",
                last_run_at=NOW - timedelta(days=1))
    session.add(PolicyRun(policy_id=p.id, status="success",
                          started_at=NOW - timedelta(days=1)))
    session.commit()
    assert missed_runs(session, now=NOW) == []
    # with a tighter explicit grace the same state flags
    flags = missed_runs(session, now=NOW, grace_min=30)
    assert len(flags) == 1 and flags[0]["grace_min"] == 30


def test_partial_renders_flags_and_degrades(session):
    _policy(session)
    r = _client().get("/panel/partials/missed")
    assert r.status_code == 200
    assert "nightly" in r.text and "never launched" in r.text
    # home page polls the missed partial
    home = _client().get("/panel")
    assert "/panel/partials/missed" in home.text and "错过的运行" in home.text


# ── 清红判据是「跑过」，不是「不是 failed」（unblock-silent-dispatch-stalls）──
# 规格原文：「the flag clears once a run for the policy records success or the
# policy is disabled」。旧写法 `status != "failed"` 把**任何**非 failed 都当成
# 已覆盖 ⇒ 两张本 change 新落下的行（skipped / stalled）会各自把红旗洗掉：
#   · `skipped`（3.x 单飞跳过留痕）——2026-11-13 那种"整跳被挡住"的情形，
#     在有了跳过行之后反而**不会**再上榜（本 change 自己造的新盲区）；
#   · `stalled`（回收器关的悬行）——pod 被 activeDeadline 杀掉、产出未知。
# 两条断言对修复前代码均为红。

def test_skipped_row_does_not_clear_the_flag(session):
    """单飞跳过的留痕不算覆盖这一跳，且理由要点名被挡住。"""
    p = _policy(session)
    session.add(PolicyRun(
        policy_id=p.id, status="skipped",
        started_at=NOW - timedelta(days=1),
        detail="skipped: blocked by open run 2383 (started 2026-06-13T22:00Z)"))
    session.commit()
    flags = missed_runs(session, now=NOW)
    assert len(flags) == 1, flags
    assert flags[0]["reason"] == "blocked by single-flight"


def test_stalled_row_does_not_clear_the_flag(session):
    """卡死行（结局未知）不算覆盖，理由要说出"没有终态报告"。"""
    p = _policy(session)
    session.add(PolicyRun(
        policy_id=p.id, status="stalled",
        started_at=NOW - timedelta(days=1),
        detail="stalled: no terminal report in 18768s > 10800s; "
               "Yield unknown, NOT zero"))
    session.commit()
    flags = missed_runs(session, now=NOW)
    assert len(flags) == 1, flags
    assert flags[0]["reason"] == "stalled without a terminal report"


def test_zero_yield_and_no_op_still_clear_the_flag(session):
    """成对臂：真正跑完的零产出/空计划仍算覆盖 —— 别把「没数据」当「没运行」。"""
    p = _policy(session)
    for status in ("zero_yield", "no_op", "redundant"):
        session.query(PolicyRun).delete()
        session.add(PolicyRun(policy_id=p.id, status=status,
                              started_at=NOW - timedelta(hours=1)))
        session.commit()
        assert missed_runs(session, now=NOW) == [], status

