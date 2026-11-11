"""A launch request whose response is lost must not be reported as a failure.

Observed live 2026-11-11 on `cheap`: `policy_runs` 2347 recorded
``launch failed: The read operation timed out`` with ``job_ref=NULL``, while the
Job the same attempt had already created (``crawl-policy-2487-20261111063707``,
its name timestamp 47 s before the pod's ``startTime``) ran to ``succeeded=1``
and wrote its rows. The ledger said failed; the data said otherwise, and that
Job had no row pointing at it.

The decision therefore has to be made by re-reading the cluster, and the three
outcomes must stay separate: exists (adopt), 404 (confirmed absent), and read
failure (unconfirmed — do NOT claim absence).
"""
import datetime as dt
import urllib.error

import pytest

from fd_open_data_mcp.refresh.reconciler import (
    LOST_RESPONSE_ADOPTION_WINDOW,
    LaunchResponseLost,
    adopt_job_if_present,
)


def _now():
    return dt.datetime(2026, 11, 11, 6, 40, tzinfo=dt.timezone.utc)


def _http_404(url="https://api/x"):
    return urllib.error.HTTPError(url, 404, "Not Found", {}, None)


class _FakeClient:
    """Minimal stand-in: only what adopt_job_if_present touches."""

    def __init__(self, job=None, error=None, cluster_name="cheap"):
        self._job = job
        self._error = error
        from types import SimpleNamespace
        self.cluster = SimpleNamespace(name=cluster_name)
        self.get_calls = 0

    def get_job(self, name):
        self.get_calls += 1
        if self._error is not None:
            raise self._error
        return self._job


def _job(created: dt.datetime):
    return {"metadata": {"name": "crawl-policy-1-x",
                         "creationTimestamp": created.isoformat().replace("+00:00", "Z")}}


def test_adopts_a_job_that_landed_before_the_timeout(caplog):
    """The whole point: an existing Job means the launch DID happen."""
    created = _now() - dt.timedelta(seconds=47)
    client = _FakeClient(job=_job(created))
    err = TimeoutError("The read operation timed out")

    with caplog.at_level("WARNING"):
        assert adopt_job_if_present(client, "crawl-policy-1-x", err, now=_now()) is None

    assert client.get_calls == 1
    warning = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("adopting" in m for m in warning), warning
    line = next(m for m in warning if "adopting" in m)
    # The signal has to survive in the log, or a lost response is
    # indistinguishable from a clean launch.
    assert "cheap" in line and "crawl-policy-1-x" in line
    assert "TimeoutError" in line and "read operation timed out" in line
    assert created.isoformat() in line


def test_confirmed_absent_is_a_clean_failure():
    """404 is the only case where 'it never ran' is actually observed."""
    client = _FakeClient(error=_http_404())

    with pytest.raises(LaunchResponseLost) as ei:
        adopt_job_if_present(client, "crawl-policy-1-x",
                             urllib.error.URLError("handshake timed out"), now=_now())

    msg = str(ei.value)
    assert "不存在" in msg
    assert "干净失败" in msg
    # the original error text stays visible for the operator
    assert "handshake timed out" in msg


def test_unreadable_cluster_does_not_claim_absence():
    """A failed re-read must never be reported as 'the Job is not there'.

    Getting this wrong in the other direction (marking the run running when no
    Job exists) dangles the row and blocks that policy's single-flight forever,
    so the honest answer here is 'unconfirmed', and the run closes as failed.
    """
    client = _FakeClient(error=urllib.error.URLError("connection refused"))

    with pytest.raises(LaunchResponseLost) as ei:
        adopt_job_if_present(client, "crawl-policy-1-x", TimeoutError("timed out"),
                             now=_now())

    msg = str(ei.value)
    assert "无法确认" in msg
    assert "不存在" not in msg


def test_stale_same_named_job_is_not_adopted():
    """Refuse a same-named Job from long ago — adopting it would hide a real failure."""
    stale = _now() - LOST_RESPONSE_ADOPTION_WINDOW - dt.timedelta(minutes=1)
    client = _FakeClient(job=_job(stale))

    with pytest.raises(LaunchResponseLost) as ei:
        adopt_job_if_present(client, "crawl-policy-1-x", TimeoutError("timed out"),
                             now=_now())

    assert "窗口" in str(ei.value)


def test_missing_creation_timestamp_is_not_adopted():
    """An unreadable timestamp is not evidence of anything."""
    client = _FakeClient(job={"metadata": {"name": "crawl-policy-1-x"}})

    with pytest.raises(LaunchResponseLost) as ei:
        adopt_job_if_present(client, "crawl-policy-1-x", TimeoutError("timed out"),
                             now=_now())

    assert "creationTimestamp" in str(ei.value)


def test_launch_does_not_resend_the_create_after_a_lost_response(monkeypatch):
    """One attempt per launch: a resend could create a second Job."""
    from types import SimpleNamespace

    from fd_open_data_mcp.refresh import reconciler

    created = dt.datetime.now(dt.timezone.utc)
    seen = {"create": 0, "get": 0}

    class _Client:
        def __init__(self, cluster):
            self.cluster = cluster

        def create(self, doc):
            seen["create"] += 1
            if doc["kind"] == "Job":
                raise TimeoutError("The read operation timed out")

        def get_job(self, name):
            seen["get"] += 1
            return _job(created)

    cluster = SimpleNamespace(id=9, name="cheap", namespace="scraw",
                              image="registry/x:tag", runtime_hints=None)
    monkeypatch.setattr(reconciler, "pick_cluster", lambda *a, **kw: cluster)
    monkeypatch.setattr(reconciler, "ClusterK8sClient", _Client)
    monkeypatch.setattr(reconciler, "_read_script", lambda path: "# stub\n")

    policy = SimpleNamespace(id=7, name="p", executor="direct",
                             script="adhoc", script_args=[])
    job_ref, cluster_id = reconciler.MultiClusterLauncher().launch(None, policy)

    assert job_ref.startswith("cheap/crawl-policy-7-")
    assert cluster_id == 9
    assert seen == {"create": 2, "get": 1}  # ConfigMap + Job once, then one re-read


def test_launch_propagates_a_confirmed_absence(monkeypatch):
    """The caller must still be able to record a failed run."""
    from types import SimpleNamespace

    from fd_open_data_mcp.refresh import reconciler

    class _Client:
        def __init__(self, cluster):
            self.cluster = cluster

        def create(self, doc):
            if doc["kind"] == "Job":
                raise TimeoutError("The read operation timed out")

        def get_job(self, name):
            raise _http_404()

    cluster = SimpleNamespace(id=9, name="cheap", namespace="scraw",
                              image="registry/x:tag", runtime_hints=None)
    monkeypatch.setattr(reconciler, "pick_cluster", lambda *a, **kw: cluster)
    monkeypatch.setattr(reconciler, "ClusterK8sClient", _Client)
    monkeypatch.setattr(reconciler, "_read_script", lambda path: "# stub\n")

    policy = SimpleNamespace(id=7, name="p", executor="direct",
                             script="adhoc", script_args=[])
    with pytest.raises(LaunchResponseLost):
        reconciler.MultiClusterLauncher().launch(None, policy)


def test_launch_direct_records_failed_when_absence_is_confirmed(session):
    """The exception → ledger hop: a confirmed absence still closes as failed.

    The point of the change is only to stop *false* failures; a launch that
    provably never created a Job must keep producing a failed run row with the
    reason, or the failure goes silent.
    """
    from fd_open_data_mcp.models import CrawlPolicy, PolicyRun
    from fd_open_data_mcp.refresh.reconciler import _launch_direct

    class _Launcher:
        def launch(self, plan, policy):
            raise LaunchResponseLost(
                "The read operation timed out — 已回合核对：Job crawl-policy-1-x"
                " 不存在（干净失败，可安全重试）")

    policy = CrawlPolicy(name="lost", enabled=True, concept_ids=[1],
                         entity_type="fund", cron_expr="0 6 * * *",
                         timezone="UTC", date_policy={"mode": "since_last"},
                         frequency="daily", mode="per_date", executor="direct",
                         script="bulk_ingest_x")
    session.add(policy)
    session.commit()

    out = _launch_direct(session, policy, _Launcher(),
                         now=dt.datetime.now(dt.timezone.utc))

    assert out["status"] == "refused"
    run = session.query(PolicyRun).filter_by(policy_id=policy.id).one()
    assert run.status == "failed"
    assert run.job_ref is None
    assert "不存在" in run.detail
