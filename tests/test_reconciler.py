"""Reconciler tests (add-fund-crawl-control-center, spec crawl-control-center).

Covers: due-policy selection (cron + timezone), per-policy single-flight,
date-range builder (since_last/trailing/explicit), plan-size guardrail with
force override, launcher abstraction, the completion probe, and the failed-
close error diagnosis (2026-10-05 incident: empty error_head on early crash).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fd_open_data_protocol.schema import (
    ColumnSpec, ConceptHint, DatasourceManifest, FunctionSpec,
)
from sqlalchemy import text

from fd_open_data_mcp.catalog.register import register_datasource
from fd_open_data_mcp.models import CrawlPolicy, PolicyRun, Schedule
from fd_open_data_mcp.refresh import reconciler
from fd_open_data_mcp.refresh.reconciler import (
    build_date_range, estimate_fetches, reconcile_once,
)

NOW = datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc)


def _register(session) -> int:
    register_datasource(DatasourceManifest(
        name="test-src", label="Test Src",
        functions=[FunctionSpec(
            command="get_hist", frequency="daily", parameters=[],
            columns=[ColumnSpec(name="close", type="float", frequency="daily")],
        )],
        concepts=[ConceptHint(column="close", concept="price.close",
                              entity_type="stock", unit="currency", frequency="daily")],
    ), session)
    from fd_open_data_mcp.models import Concept
    return session.query(Concept).filter_by(code="price.close", entity_type="stock").first().id


def _policy(session, cid, **over) -> CrawlPolicy:
    kw = dict(
        name="p1", enabled=True, concept_ids=[cid], entity_type="stock",
        entity_ids=[1, 2], date_policy={"mode": "explicit", "start": "2025-06-01"},
        frequency="daily", mode="per_date", cron_expr="0 * * * *", timezone="UTC",
        last_run_at=NOW - timedelta(hours=2),   # hourly cron -> due at NOW
    )
    kw.update(over)
    p = CrawlPolicy(**kw)
    session.add(p)
    session.commit()
    return p


class _FakeLauncher:
    def __init__(self, poll_state="unknown", diagnose_result=None):
        self.launched: list[tuple] = []
        self.poll_state = poll_state
        self.diagnose_result = diagnose_result
        self.diagnosed: list[str] = []

    def launch(self, plan, policy):
        self.launched.append((plan, policy))
        # Launcher Protocol returns (job_ref, cluster_id); None = no cluster
        # (scrapyd/legacy single-cluster path).
        return (f"job-{len(self.launched)}", None)

    def poll(self, job_ref):
        return self.poll_state

    def diagnose(self, job_ref):
        # K8s launchers expose pod terminal-state evidence on the failed close
        # path (2026-10-05 incident); None = pod already reclaimed.
        self.diagnosed.append(job_ref)
        return self.diagnose_result


# ─── due selection + single-flight (5.1) ─────────────────────────────────────
def test_due_policy_launches_and_records_run(session):
    cid = _register(session)
    p = _policy(session, cid)
    launcher = _FakeLauncher()
    summary = reconcile_once(session, launcher, now=NOW)
    assert [l["policy"] for l in summary["launched"]] == ["p1"]
    run = session.query(PolicyRun).filter_by(policy_id=p.id).one()
    assert run.status == "running"
    assert run.job_ref == "job-1"
    assert run.plan_json["wanted_concepts"][0]["concept_id"] == cid
    # SQLite strips tzinfo; compare in UTC
    assert p.last_run_at.replace(tzinfo=timezone.utc) == NOW


def test_not_due_policy_is_skipped(session):
    cid = _register(session)
    # hourly cron fired at 12:00 exactly -> next fire 13:00 > NOW
    _policy(session, cid, last_run_at=NOW)
    launcher = _FakeLauncher()
    summary = reconcile_once(session, launcher, now=NOW)
    assert summary["launched"] == [] and launcher.launched == []


def test_disabled_policy_is_skipped(session):
    cid = _register(session)
    _policy(session, cid, enabled=False)
    launcher = _FakeLauncher()
    assert reconcile_once(session, launcher, now=NOW)["launched"] == []


def test_single_flight_blocks_overlap(session):
    cid = _register(session)
    p = _policy(session, cid)
    session.add(PolicyRun(policy_id=p.id, status="running", job_ref="old-job",
                          started_at=NOW - timedelta(hours=1)))
    session.commit()
    launcher = _FakeLauncher()  # poll returns "unknown" -> old run stays open
    summary = reconcile_once(session, launcher, now=NOW)
    assert summary["launched"] == []
    assert summary["skipped"][0]["policy"] == "p1"
    assert "single-flight" in summary["skipped"][0]["reason"]


def test_completion_probe_closes_finished_runs(session):
    cid = _register(session)
    p = _policy(session, cid, cron_expr="0 0 1 1 *")  # yearly, not due
    run = PolicyRun(policy_id=p.id, status="running", job_ref="done-job",
                    started_at=NOW - timedelta(hours=3))
    session.add(run)
    session.commit()
    launcher = _FakeLauncher(poll_state="success")
    summary = reconcile_once(session, launcher, now=NOW)
    assert summary["probed_closed"] == 1
    # fix-silent-zero-yield-crawls D3: a succeeded job that never reported
    # yield counters closes zero_yield — exit code 0 no longer means "success"
    assert run.status == "zero_yield"
    assert run.finished_at.replace(tzinfo=timezone.utc) == NOW

    # ...and one that DID report counters closes on its yield
    ok = PolicyRun(policy_id=p.id, status="running", job_ref="done-job-2",
                   started_at=NOW - timedelta(hours=3),
                   plan_cells=10, rows_attempted=10, rows_new=4)
    session.add(ok)
    session.commit()
    summary = reconcile_once(session, _FakeLauncher(poll_state="success"), now=NOW)
    assert summary["probed_closed"] == 1
    assert ok.status == "success"


# ─── failed-close error diagnosis (2026-10-05 incident) ──────────────────────
def test_failed_job_without_checkin_synthesizes_error_detail(session):
    """Job failed + no runner yield report -> a non-empty summary carrying the
    pod's exit code / log tail lands on policy_runs.detail AND, through the
    close mirror, on crawl_runs.error_head (which was empty in the incident)."""
    from fd_open_data_mcp.models import CrawlRun

    cid = _register(session)
    p = _policy(session, cid, cron_expr="0 0 1 1 *",  # yearly: probe only, no relaunch
                source_filter=["test-src"])
    run = PolicyRun(policy_id=p.id, status="running", job_ref="crawl-policy-1-x",
                    started_at=NOW - timedelta(hours=3))
    session.add(run)
    session.commit()
    diag = ("job crawl-policy-1-x failed before the runner checked in; "
            "pod scraw/crawl-policy-1-x-abcde container=crawler exitCode=1 "
            'reason=Error message="ModuleNotFoundError: No module named scrapy"')
    launcher = _FakeLauncher(poll_state="failed", diagnose_result=diag)
    summary = reconcile_once(session, launcher, now=NOW)
    assert summary["probed_closed"] == 1
    assert run.status == "failed"
    assert launcher.diagnosed == ["crawl-policy-1-x"]
    assert run.detail and "exitCode=1" in run.detail
    assert "ModuleNotFoundError" in run.detail
    row = session.query(CrawlRun).filter_by(source="test-src", kind="concept").one()
    assert row.status == "failed"
    assert row.error_head and "exitCode=1" in row.error_head


def test_runner_reported_failed_run_not_overwritten(session):
    """A runner that checked in (counters present) is never re-synthesized:
    diagnose is not even attempted and a pre-existing detail stays untouched."""
    from fd_open_data_mcp.models import CrawlRun

    cid = _register(session)
    p = _policy(session, cid, cron_expr="0 0 1 1 *", source_filter=["test-src"])
    run = PolicyRun(policy_id=p.id, status="running", job_ref="crawl-policy-1-y",
                    started_at=NOW - timedelta(hours=3),
                    rows_attempted=42, rows_new=17)
    session.add(run)
    session.commit()
    launcher = _FakeLauncher(poll_state="failed",
                             diagnose_result="POD EVIDENCE MUST NOT APPEAR")
    summary = reconcile_once(session, launcher, now=NOW)
    assert summary["probed_closed"] == 1
    assert run.status == "failed"
    assert run.detail is None                    # nothing synthesized
    assert launcher.diagnosed == []              # diagnosis not even attempted
    row = session.query(CrawlRun).filter_by(source="test-src", kind="concept").one()
    assert not row.error_head


def test_reclaimed_pod_falls_back_to_nonempty_note(session):
    """diagnose finds nothing (pod TTL-collected) -> a terse non-empty note
    with the job name, so a failed close never carries an empty summary."""
    cid = _register(session)
    p = _policy(session, cid, cron_expr="0 0 1 1 *")
    run = PolicyRun(policy_id=p.id, status="running", job_ref="crawl-policy-9-gone",
                    started_at=NOW - timedelta(hours=3))
    session.add(run)
    session.commit()
    reconcile_once(session, _FakeLauncher(poll_state="failed", diagnose_result=None),
                   now=NOW)
    assert run.status == "failed"
    assert run.detail and "crawl-policy-9-gone" in run.detail
    assert "pod 已回收" in run.detail


def test_legacy_schedules_table_is_not_executed(session):
    cid = _register(session)
    session.add(Schedule(concept_id=cid, cron_expr="* * * * *", timezone="UTC", enabled=True))
    session.commit()
    launcher = _FakeLauncher()
    assert reconcile_once(session, launcher, now=NOW)["launched"] == []
    assert launcher.launched == []


# ─── date-range builder (5.2) ────────────────────────────────────────────────
def test_trailing_date_policy(session):
    cid = _register(session)
    p = _policy(session, cid, date_policy={"mode": "trailing", "days": 7})
    dr, since_last = build_date_range(p, NOW.date())
    assert not since_last
    assert dr.start == "2025-06-08" and dr.end == "2025-06-15"


def test_launch_uses_policy_local_today(session):
    """At 17:00 UTC (= 01:00 next-day Beijing), a trailing policy's range end is
    Beijing's calendar day, not UTC's (fix-observation-time-granularity,
    spec crawl-control-center delta)."""
    cid = _register(session)
    now_cn = datetime(2025, 8, 9, 17, 0, tzinfo=timezone.utc)  # 2025-08-10 01:00 Beijing
    p = _policy(session, cid,
                date_policy={"mode": "trailing", "days": 1},
                timezone="Asia/Shanghai",
                last_run_at=now_cn - timedelta(hours=1),
                cron_expr="0 1 * * *")  # fires 01:00 local
    launcher = _FakeLauncher()
    result = reconciler.launch_policy(session, p, launcher, now=now_cn)
    assert result["status"] == "launched"
    plan = launcher.launched[0][0]
    assert plan.date_range.end == "2025-08-10"      # Beijing today, not UTC 08-09
    assert plan.date_range.start == "2025-08-09"    # trailing 1: [Aug 9, Aug 10]


def test_launch_utc_policy_uses_utc_today(session):
    """A UTC policy is unchanged: the range end is UTC's calendar day."""
    cid = _register(session)
    now = datetime(2025, 8, 9, 17, 0, tzinfo=timezone.utc)
    p = _policy(session, cid,
                date_policy={"mode": "trailing", "days": 1},
                timezone="UTC",
                last_run_at=now - timedelta(hours=1),
                cron_expr="0 * * * *")
    launcher = _FakeLauncher()
    result = reconciler.launch_policy(session, p, launcher, now=now)
    assert result["status"] == "launched"
    plan = launcher.launched[0][0]
    assert plan.date_range.end == "2025-08-09"      # UTC today


def test_explicit_date_policy(session):
    cid = _register(session)
    p = _policy(session, cid, date_policy={"mode": "explicit", "start": "2020-01-01",
                                           "end": "2020-12-31"})
    dr, since_last = build_date_range(p, NOW.date())
    assert not since_last
    assert dr.start == "2020-01-01" and dr.end == "2020-12-31"


def test_since_last_derives_from_watermark(session):
    cid = _register(session)
    session.execute(text(
        "INSERT INTO semantic_observations (concept_id, entity_type, entity_id, date, value, source_used, fetched_at) "
        "VALUES (:c, 'stock', 1, '2025-06-10', '100', 'test-src', :now)"
    ), {"c": cid, "now": NOW})
    session.commit()
    p = _policy(session, cid, date_policy={"mode": "since_last"})
    launcher = _FakeLauncher()
    summary = reconcile_once(session, launcher, now=NOW)
    plan, _ = launcher.launched[0]
    # watermark 2025-06-10 + one daily period -> 2025-06-11
    assert plan.date_range.start == "2025-06-11"
    assert plan.date_range.end == "2025-06-15"
    assert summary["launched"]


# ─── plan-size guardrail (5.3) ───────────────────────────────────────────────
def test_oversized_plan_is_refused(session, monkeypatch):
    cid = _register(session)
    # 2 entities x 15 days (Jun 1..15) = 30 fetches > ceiling 10
    monkeypatch.setattr(reconciler, "POLICY_MAX_FETCHES", 10)
    p = _policy(session, cid)
    launcher = _FakeLauncher()
    summary = reconcile_once(session, launcher, now=NOW)
    assert launcher.launched == []
    assert summary["refused"][0]["estimate"] == 30
    run = session.query(PolicyRun).filter_by(policy_id=p.id).one()
    assert run.status == "failed"
    assert "30" in run.detail and "POLICY_MAX_FETCHES" in run.detail


def test_force_overrides_guardrail(session, monkeypatch):
    cid = _register(session)
    monkeypatch.setattr(reconciler, "POLICY_MAX_FETCHES", 10)
    _policy(session, cid, force=True)
    launcher = _FakeLauncher()
    summary = reconcile_once(session, launcher, now=NOW)
    assert len(summary["launched"]) == 1
    assert summary["launched"][0]["estimate"] == 30


def test_series_mode_estimates_one_fetch_per_entity(session):
    cid = _register(session)
    _policy(session, cid, mode="series",
            date_policy={"mode": "explicit", "start": "2020-01-01", "end": "2025-12-31"})
    # series would refuse: get_hist is not bulk_history -> unroutable, estimate 0
    launcher = _FakeLauncher()
    summary = reconcile_once(session, launcher, now=NOW)
    plan, _ = launcher.launched[0]
    assert plan.unroutable[0]["reason"] == "no bulk_history source for series mode"
    assert estimate_fetches(session, plan) == 0


# ─── K8sJobLauncher transports (5.4) ─────────────────────────────────────────
def _crawl_plan():
    from fd_open_data_mcp.crawl.plan import CrawlPlan
    return CrawlPlan.model_validate({
        "version": "1", "mode": "per_date",
        "wanted_concepts": [], "entity_scope": {"entity_type": "fund"},
        "date_range": {"start": "2025-06-01", "end": "2025-06-15", "frequency": "daily"},
        "unroutable": [], "unmapped": [],
        "persistence": {"table": "semantic_observations"},
    })


def test_k8s_launcher_in_cluster_posts_configmap_then_job(monkeypatch):
    from types import SimpleNamespace

    from fd_open_data_mcp.refresh.reconciler import K8sJobLauncher
    launcher = K8sJobLauncher(namespace="scraw")
    monkeypatch.setattr(K8sJobLauncher, "_in_cluster", lambda self: True)
    calls = []
    monkeypatch.setattr(K8sJobLauncher, "_k8s_api",
                        lambda self, m, p, b=None: calls.append((m, p, b)) or {})

    name, _cluster_id = launcher.launch(_crawl_plan(), SimpleNamespace(id=7))
    assert name.startswith("crawl-policy-7-")
    (m1, p1, cm), (m2, p2, job) = calls
    assert (m1, p1) == ("POST", "/api/v1/namespaces/scraw/configmaps")
    assert (m2, p2) == ("POST", "/apis/batch/v1/namespaces/scraw/jobs")
    assert cm["metadata"]["name"] == f"{name}-plan"
    assert "plan.json" in cm["data"]
    assert job["metadata"]["name"] == name
    assert job["spec"]["template"]["spec"]["containers"][0]["command"] == [
        "scraw-fd-open-data-mcp", "crawl", "/plan/plan.json"]


@pytest.mark.parametrize("status,expected", [
    ({"active": 1}, "running"),
    ({"succeeded": 1}, "success"),
    ({"failed": 2}, "failed"),
    ({}, "unknown"),
])
def test_k8s_launcher_poll_in_cluster(monkeypatch, status, expected):
    from fd_open_data_mcp.refresh.reconciler import K8sJobLauncher
    launcher = K8sJobLauncher()
    monkeypatch.setattr(K8sJobLauncher, "_in_cluster", lambda self: True)
    monkeypatch.setattr(K8sJobLauncher, "_k8s_api",
                        lambda self, m, p, b=None: {"status": status})
    assert launcher.poll("crawl-policy-7-x") == expected


def _crashed_pod(name="crawl-policy-7-x-abcde"):
    """The incident's pod shape: restartPolicy=OnFailure restarted the dead
    container, so the crash evidence lives in lastState.terminated."""
    return {
        "metadata": {"name": name},
        "status": {"containerStatuses": [{
            "name": "crawler",
            "state": {"waiting": {"reason": "CrashLoopBackOff"}},
            "lastState": {"terminated": {
                "exitCode": 1, "reason": "Error",
                "message": "scrapy import failed"}},
        }]},
    }


def test_k8s_launcher_diagnose_synthesizes_pod_evidence(monkeypatch):
    """diagnose reads lastState.terminated + the pod log tail; the log endpoint
    returns text/plain, so it goes through the raw-text API call."""
    from fd_open_data_mcp.refresh.reconciler import K8sJobLauncher

    launcher = K8sJobLauncher(namespace="scraw")
    monkeypatch.setattr(K8sJobLauncher, "_in_cluster", lambda self: True)
    calls = []
    monkeypatch.setattr(K8sJobLauncher, "_k8s_api",
                        lambda self, m, p, b=None:
                        calls.append((m, p)) or {"items": [_crashed_pod()]})
    monkeypatch.setattr(
        K8sJobLauncher, "_k8s_api_text",
        lambda self, p: calls.append(("TEXT", p))
        or "Traceback (most recent call last):\n"
           "ModuleNotFoundError: No module named 'scrapy'")

    out = launcher.diagnose("crawl-policy-7-x")
    assert out and "exitCode=1" in out and "reason=Error" in out
    assert "crawl-policy-7-x-abcde" in out
    assert "ModuleNotFoundError" in out          # log tail included
    text_path = next(p for m, p in calls if m == "TEXT")
    assert "/pods/crawl-policy-7-x-abcde/log" in text_path
    assert "tailLines=30" in text_path and "container=crawler" in text_path
    get_path = next(p for m, p in calls if m == "GET")
    # pods are found via the Job controller's automatic job-name label
    assert get_path.startswith("/api/v1/namespaces/scraw/pods?labelSelector=")
    assert "job-name%3Dcrawl-policy-7-x" in get_path


def test_k8s_launcher_diagnose_returns_none_when_api_fails(monkeypatch):
    from fd_open_data_mcp.refresh.reconciler import K8sJobLauncher

    launcher = K8sJobLauncher(namespace="scraw")
    monkeypatch.setattr(K8sJobLauncher, "_in_cluster", lambda self: True)

    def boom(self, m, p, b=None):
        raise RuntimeError("api unreachable")

    monkeypatch.setattr(K8sJobLauncher, "_k8s_api", boom)
    assert launcher.diagnose("crawl-policy-7-x") is None


def test_multicluster_launcher_diagnose_routes_to_cluster(session, monkeypatch):
    """The dispatch path ("{cluster}/{job}") resolves the cluster row and
    diagnoses via that cluster's API client."""
    from fd_open_data_mcp.models import Cluster
    from fd_open_data_mcp.refresh.reconciler import ClusterK8sClient, MultiClusterLauncher

    session.add(Cluster(name="tencent", api_server="https://tencent:6443",
                        namespace="scraw", capacity=2))
    session.commit()
    monkeypatch.setattr(ClusterK8sClient, "pods_for_job",
                        lambda self, name: [_crashed_pod("crawl-policy-5-x-z")])
    monkeypatch.setattr(ClusterK8sClient, "pod_log_tail",
                        lambda self, pod, lines=30: "scrapy: crash on import")

    out = MultiClusterLauncher().diagnose("tencent/crawl-policy-5-x")
    assert out and "exitCode=1" in out and "crawl-policy-5-x-z" in out
    assert "scrapy: crash on import" in out
    # unknown cluster / scrapyd-style refs degrade to "no evidence" (the close
    # path then writes its non-empty fallback note)
    assert MultiClusterLauncher().diagnose("ghost/crawl-policy-5-x") is None
    assert MultiClusterLauncher().diagnose("no-cluster-prefix") is None


# ── census auto-refresh hook (add-census-auto-refresh) ───────────────────────

def test_reconcile_refreshes_absent_census(session, monkeypatch):
    """No census rows -> the tick refreshes (with a disabled policy set)."""
    from unittest.mock import patch
    from fd_open_data_mcp.refresh import reconciler
    from fd_open_data_mcp.visibility import census as census_mod

    p = _policy(session, _register(session), enabled=False)
    calls = []
    with patch.object(census_mod, "refresh_census", lambda s: calls.append(s)):
        reconciler.reconcile_once(session, _FakeLauncher(), now=NOW)
    assert len(calls) == 1


def test_reconcile_refreshes_stale_census(session, monkeypatch):
    import datetime as dt
    from unittest.mock import patch
    from fd_open_data_mcp.models import DataCensus
    from fd_open_data_mcp.refresh import reconciler
    from fd_open_data_mcp.visibility import census as census_mod

    session.add(DataCensus(store="gz_master", kind="local", exact=True,
                           sampled_at=dt.datetime.utcnow() - dt.timedelta(hours=48)))
    session.commit()
    calls = []
    with patch.object(census_mod, "refresh_census", lambda s: calls.append(s)):
        reconciler.reconcile_once(session, _FakeLauncher(), now=NOW)
    assert len(calls) == 1


def test_reconcile_skips_fresh_census(session, monkeypatch):
    import datetime as dt
    from unittest.mock import patch
    from fd_open_data_mcp.models import DataCensus
    from fd_open_data_mcp.refresh import reconciler
    from fd_open_data_mcp.visibility import census as census_mod

    session.add(DataCensus(store="gz_master", kind="local", exact=True,
                           sampled_at=dt.datetime.utcnow()))
    session.commit()
    calls = []
    with patch.object(census_mod, "refresh_census", lambda s: calls.append(s)):
        reconciler.reconcile_once(session, _FakeLauncher(), now=NOW)
    assert calls == []


def test_census_failure_does_not_abort_tick(session, monkeypatch):
    """A broken census (e.g. unmigrated DB) leaves the tick's launches intact."""
    from unittest.mock import patch
    from fd_open_data_mcp.refresh import reconciler
    from fd_open_data_mcp.visibility import census as census_mod

    def boom(_s):
        raise RuntimeError("no such table data_census")

    with patch.object(census_mod, "refresh_census", boom):
        summary = reconciler.reconcile_once(session, _FakeLauncher(), now=NOW)
    assert "launched" in summary  # tick completed normally
