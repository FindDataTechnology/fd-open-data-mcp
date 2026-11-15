"""Tests for the failure + stale-run scan (add-crawl-visibility).

Proves: exactly the un-alerted events are batched into one message, the
watermark advances, already-alerted runs are skipped, and the watcher never
mutates a run's status (read-only).
"""
from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock, patch

import pytest

from fd_open_data_mcp.models import CrawlPolicy, PolicyRun
from fd_open_data_mcp.visibility import scan, state


def _mk_policy(session, name="p1", **kw) -> CrawlPolicy:
    defaults = dict(
        name=name, enabled=True, concept_ids=[1], entity_type="fund",
        cron_expr="0 6 * * *", timezone="UTC",
        date_policy={"mode": "since_last"}, frequency="daily", mode="per_date",
    )
    defaults.update(kw)
    p = CrawlPolicy(**defaults)
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


def _mk_run(session, policy, status, *, finished=None, detail=None,
            started=None, plan_json=None) -> PolicyRun:
    now = dt.datetime.now(dt.timezone.utc)
    r = PolicyRun(
        policy_id=policy.id, status=status,
        started_at=started or now, finished_at=finished,
        detail=detail, plan_json=plan_json,
    )
    session.add(r)
    session.commit()
    session.refresh(r)
    return r


@pytest.fixture
def no_redis(monkeypatch):
    """State helpers without Redis: alert-at-least-once mode + no watermark."""
    monkeypatch.delenv("REDIS_URL", raising=False)
    state._REDIS = None
    # snapshot's _probe_cluster would try a real k8s call; stub fleet off
    yield


@pytest.fixture
def stub_probe(monkeypatch):
    """Avoid real k8s API probes in fleet_health."""
    from fd_open_data_mcp.visibility import snapshot
    monkeypatch.setattr(snapshot, "_probe_cluster", lambda c: True)


def test_failed_run_alerted_once(session, no_redis, stub_probe, monkeypatch):
    """A new failed run is batched into one message and marked alerted."""
    pol = _mk_policy(session)
    _mk_run(session, pol, "failed", finished=dt.datetime.now(dt.timezone.utc),
            detail="no eligible cluster")
    sent = []
    monkeypatch.setattr(scan, "get_notifier",
                        lambda: MagicMock(send=lambda title, body, *, level="info": sent.append((title, body))))
    # patch state dedup to simulate the alerted-set
    alerted = set()
    monkeypatch.setattr(state, "already_alerted", lambda rid, ev: (rid, ev) in alerted)
    monkeypatch.setattr(state, "mark_alerted", lambda rid, ev: alerted.add((rid, ev)))

    summary = scan.scan_once(session)

    assert len(sent) == 1  # one batched message
    assert summary["notified"] is True
    assert len(summary["failed"]) == 1
    assert summary["failed"][0]["policy"] == "p1"


def test_already_alerted_skipped(session, no_redis, stub_probe, monkeypatch):
    """An already-alerted failed run is not re-sent."""
    pol = _mk_policy(session, name="p2")
    _mk_run(session, pol, "failed", finished=dt.datetime.now(dt.timezone.utc))
    sent = []
    monkeypatch.setattr(scan, "get_notifier",
                        lambda: MagicMock(send=lambda title, body, *, level="info": sent.append((title, body))))
    # simulate this run already alerted as 'failed'
    monkeypatch.setattr(state, "already_alerted", lambda rid, ev: ev == "failed")
    monkeypatch.setattr(state, "mark_alerted", lambda rid, ev: None)

    summary = scan.scan_once(session)
    assert len(sent) == 0
    assert summary["notified"] is False
    assert summary["failed"] == []


def test_multiple_failures_batched_into_one_message(session, no_redis, stub_probe, monkeypatch):
    """Three failures in one window → one message, not three."""
    pol = _mk_policy(session)
    for i in range(3):
        _mk_run(session, pol, "failed",
                finished=dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=i),
                detail=f"err {i}")
    sent = []
    monkeypatch.setattr(scan, "get_notifier",
                        lambda: MagicMock(send=lambda title, body, *, level="info": sent.append((title, body))))
    monkeypatch.setattr(state, "already_alerted", lambda rid, ev: False)
    monkeypatch.setattr(state, "mark_alerted", lambda rid, ev: None)

    summary = scan.scan_once(session)
    assert len(sent) == 1
    assert len(summary["failed"]) == 3


def test_refused_classified_separately(session, no_redis, stub_probe, monkeypatch):
    """A guardrail refusal (detail prefix 'refused:') is event 'refused', not 'failed'."""
    pol = _mk_policy(session, name="refused-pol")
    _mk_run(session, pol, "failed", finished=dt.datetime.now(dt.timezone.utc),
            detail="refused: estimated 999999 fetches exceeds POLICY_MAX_FETCHES=50000")
    sent = []
    monkeypatch.setattr(scan, "get_notifier",
                        lambda: MagicMock(send=lambda title, body, *, level="info": sent.append((title, body))))
    monkeypatch.setattr(state, "already_alerted", lambda rid, ev: False)
    monkeypatch.setattr(state, "mark_alerted", lambda rid, ev: None)

    summary = scan.scan_once(session)
    assert summary["refused"] and not summary["failed"]
    assert summary["refused"][0]["policy"] == "refused-pol"


def test_stale_run_flagged_not_closed(session, no_redis, stub_probe, monkeypatch):
    """A run running >90 min is flagged stale but its status is NOT mutated."""
    pol = _mk_policy(session, name="stale-pol")
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=120)
    run = _mk_run(session, pol, "running", started=old)
    sent = []
    monkeypatch.setattr(scan, "get_notifier",
                        lambda: MagicMock(send=lambda title, body, *, level="info": sent.append((title, body))))
    monkeypatch.setattr(state, "already_alerted", lambda rid, ev: False)
    monkeypatch.setattr(state, "mark_alerted", lambda rid, ev: None)

    summary = scan.scan_once(session)
    assert summary["stale"], "expected a stale run"
    assert summary["stale"][0]["age_minutes"] >= 120
    # read-only: the run's status is unchanged
    session.expire_all()
    assert session.query(PolicyRun).get(run.id).status == "running"


def test_watermark_advances(session, no_redis, stub_probe, monkeypatch):
    """After a scan covering terminal runs, the watermark is advanced."""
    pol = _mk_policy(session)
    _mk_run(session, pol, "failed", finished=dt.datetime.now(dt.timezone.utc))
    monkeypatch.setattr(scan, "get_notifier", lambda: MagicMock(send=lambda *a, **k: None))
    monkeypatch.setattr(state, "already_alerted", lambda rid, ev: False)
    monkeypatch.setattr(state, "mark_alerted", lambda rid, ev: None)
    advanced = {}
    monkeypatch.setattr(state, "set_scan_watermark", lambda ts: advanced.setdefault("ts", ts))

    scan.scan_once(session)
    assert "ts" in advanced, "watermark was not advanced after covering terminal runs"


# ── stalled 必须可告警（unblock-silent-dispatch-stalls）──────────────────────


def test_stalled_run_is_alerted(session, no_redis, stub_probe, monkeypatch):
    """回收器关成 `stalled` 的行必须进告警。

    2026-11-13 实历：policy 2659 的 Job 被 activeDeadlineSeconds 杀掉、行留在
    running 上 3.6h。修复前这一档**既不在 ALERT_STATUSES 也不在 NO_SIGNAL**，
    而扫描的取数 SQL 是 `status.in_(ALERT_STATUSES)` ⇒ 那一行根本不会被捞出来，
    于是"回收器把悬行关掉"与"卡死没人知道"在告警面上完全同形。
    """
    pol = _mk_policy(session, name="p2659")
    _mk_run(session, pol, "stalled", finished=dt.datetime.now(dt.timezone.utc),
            detail="stalled: row open 18768s vs the executor ceiling 10800s")
    sent = []
    monkeypatch.setattr(scan, "get_notifier",
                        lambda: MagicMock(send=lambda title, body, *, level="info": sent.append((title, body))))
    alerted = set()
    monkeypatch.setattr(state, "already_alerted", lambda rid, ev: (rid, ev) in alerted)
    monkeypatch.setattr(state, "mark_alerted", lambda rid, ev: alerted.add((rid, ev)))

    summary = scan.scan_once(session)

    assert summary["notified"] is True
    assert [r["policy"] for r in summary["failed"]] == ["p2659"]
    # 事件名与 failed 分开（去重键是 run_id+event），但同在失败栏渲染
    assert summary["failed"][0]["event"] == "stalled"
    assert summary["failed"][0]["status"] == "stalled"
    assert "stalled" in sent[0][1], "detail 首行必须把 stalled 本身念出来"


def test_skipped_run_is_not_alerted(session, no_redis, stub_probe, monkeypatch):
    """`skipped` 是"这一跳被挡住"的记录，不是一次失败——不报警。

    算成失败会让同一次停摆记两次（挡住它的那条 open run 自己会报），
    连续被挡还会把失败率撑成 100%。
    """
    pol = _mk_policy(session, name="p-skip")
    _mk_run(session, pol, "skipped", finished=dt.datetime.now(dt.timezone.utc),
            detail="skipped: open run 2383 (single-flight)")
    sent = []
    monkeypatch.setattr(scan, "get_notifier",
                        lambda: MagicMock(send=lambda *a, **k: sent.append(a)))
    monkeypatch.setattr(state, "already_alerted", lambda rid, ev: False)
    monkeypatch.setattr(state, "mark_alerted", lambda rid, ev: None)

    summary = scan.scan_once(session)
    assert sent == []
    assert summary["failed"] == [] and summary["zero_yield"] == []


def test_alert_and_no_signal_vocabularies_cover_the_run_states():
    """词表钉死：新增终态必须落进某一档，不能两边都不在。

    这条是"闭集没人驱动就会漂移"的护栏：`policy_runs.status` 出现过
    running/success/failed/no_op/redundant/zero_yield/cancelled/stalled/skipped，
    其中 cancelled 既不在告警也不在无信号（面板 unknown 计数兜住它，属于已知）。
    """
    assert "stalled" in scan.ALERT_STATUSES
    assert "zero_yield" in scan.ALERT_STATUSES
    assert "skipped" in scan.NO_SIGNAL_STATUSES
    # 两档不许重叠：否则同一行既算失败又算无信号
    assert not set(scan.ALERT_STATUSES) & set(scan.NO_SIGNAL_STATUSES)
