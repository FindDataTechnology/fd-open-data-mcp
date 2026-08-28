"""Direct-executor tests (add-direct-script-executor).

Covers: refusal of a script-less direct policy (with the `refused:` run row),
the direct manifest shape (ConfigMap script, --db-url injection, script_args,
shared env + SCRAW_JOB_REF), and that direct runs flow through the same yield
classification as Scrapy runs.
"""
from __future__ import annotations

import datetime as dt

import pytest

from fd_open_data_mcp.models import Cluster, CrawlPolicy, PolicyRun
from fd_open_data_mcp.refresh.reconciler import (
    MultiClusterLauncher, _launch_direct, classify_yield,
)


@pytest.fixture
def cluster_row(session):
    row = Cluster(id=3, name="aliyun", api_server="https://aliyun:6443")
    session.add(row)
    session.commit()
    return row


class _Cluster:
    id = 3
    name = "aliyun"
    namespace = "scraw"
    image = "harbor.local/x/scraw-fd-open-data-mcp:latest"


def _mk_policy(session, name="d1", **kw) -> CrawlPolicy:
    defaults = dict(
        name=name, enabled=True, concept_ids=[1], entity_type="fund",
        cron_expr="0 6 * * *", timezone="UTC",
        date_policy={"mode": "since_last"}, frequency="daily", mode="per_date",
        executor="direct",
    )
    defaults.update(kw)
    p = CrawlPolicy(**defaults)
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


class _RecordingLauncher(MultiClusterLauncher):
    """Capture manifests without touching a cluster."""

    def __init__(self):
        super().__init__(database_url="postgresql+psycopg2://u:p@db/db",
                         redis_url="redis://r:6379/0")
        self.created = []

    def launch(self, plan, policy):
        self.manifests = self._direct_manifests("crawl-policy-9-1", policy, _Cluster())
        # emulate the SCRAW_JOB_REF injection the real launch() performs
        job_ref = f"{_Cluster().name}/crawl-policy-9-1"
        for doc in self.manifests:
            if doc["kind"] == "Job":
                for c in doc["spec"]["template"]["spec"]["containers"]:
                    c["env"].append({"name": "SCRAW_JOB_REF", "value": job_ref})
        return (job_ref, _Cluster().id)


def test_direct_policy_without_script_is_refused(session):
    pol = _mk_policy(session, "no-script", script=None)
    result = _launch_direct(session, pol, _RecordingLauncher(),
                            now=dt.datetime.now(dt.timezone.utc))
    assert result["status"] == "refused"
    run = session.query(PolicyRun).filter_by(policy_id=pol.id).one()
    assert run.status == "failed"
    assert run.detail.startswith("refused:")
    assert "no script" in run.detail


def test_direct_policy_launches_and_tracks_run(session, monkeypatch, cluster_row):
    from fd_open_data_mcp.refresh import reconciler
    monkeypatch.setattr(reconciler, "_read_script",
                        lambda p: "# script source\n")
    pol = _mk_policy(session, "with-script", script="bulk_ingest_fund_nav_daily",
                     script_args=["--start-year", "2026"])
    launcher = _RecordingLauncher()
    result = _launch_direct(session, pol, launcher,
                            now=dt.datetime.now(dt.timezone.utc))
    assert result["status"] == "launched"
    assert result["executor"] == "direct"
    run = session.query(PolicyRun).filter_by(policy_id=pol.id).one()
    assert run.status == "running"
    assert run.job_ref == "aliyun/crawl-policy-9-1"
    assert run.cluster_id == 3


def test_direct_manifest_shape(session, monkeypatch):
    from fd_open_data_mcp.refresh import reconciler
    monkeypatch.setattr(reconciler, "_read_script",
                        lambda p: "# script source\nprint('x')\n")
    launcher = _RecordingLauncher()
    pol = _mk_policy(session, "shape", script="bulk_ingest_fund_nav_daily",
                     script_args=["--start-year", "2026"])
    launcher.launch(None, pol)
    cm, job = launcher.manifests
    assert cm["kind"] == "ConfigMap"
    assert "bulk_ingest_fund_nav_daily.py" in cm["data"]
    assert cm["data"]["bulk_ingest_fund_nav_daily.py"].startswith("# script source")
    cmd = job["spec"]["template"]["spec"]["containers"][0]["command"][-1]
    assert "bulk_ingest_fund_nav_daily.py" in cmd
    assert "--start-year 2026" in cmd
    assert '--db-url "$FD_OPEN_DATA_MCP_DATABASE_URL"' in cmd
    env = {e["name"]: e["value"] for e in
           job["spec"]["template"]["spec"]["containers"][0]["env"]
           if "value" in e}
    assert env["SCRAW_CLUSTER_ID"] == "3"
    assert env["SCRAW_CLUSTER_NAME"] == "aliyun"
    assert env["SCRAW_JOB_REF"] == "aliyun/crawl-policy-9-1"
    assert env["FD_PROXY_FORWARDER"].startswith("http://$(K8S_NODE_IP)")


def test_silent_direct_run_classifies_zero_yield(session, monkeypatch, cluster_row):
    from fd_open_data_mcp.refresh import reconciler
    monkeypatch.setattr(reconciler, "_read_script", lambda p: "# s\n")
    # job succeeded but the script never reported counters -> zero_yield
    pol = _mk_policy(session, "silent", script="bulk_ingest_fund_nav_daily")
    _launch_direct(session, pol, _RecordingLauncher(),
                   now=dt.datetime.now(dt.timezone.utc))
    run = session.query(PolicyRun).filter_by(policy_id=pol.id).one()
    run.status = "running"  # simulate the reconciler's success probe path
    assert run.rows_attempted is None and run.rows_new is None
    assert classify_yield(run) == "zero_yield"


def test_reporting_direct_run_classifies_success(session, monkeypatch, cluster_row):
    from fd_open_data_mcp.refresh import reconciler
    monkeypatch.setattr(reconciler, "_read_script", lambda p: "# s\n")
    pol = _mk_policy(session, "reporting", script="bulk_ingest_fund_nav_daily")
    _launch_direct(session, pol, _RecordingLauncher(),
                   now=dt.datetime.now(dt.timezone.utc))
    run = session.query(PolicyRun).filter_by(policy_id=pol.id).one()
    run.rows_attempted, run.rows_new = 23897, 23897  # script reported
    assert classify_yield(run) == "success"
