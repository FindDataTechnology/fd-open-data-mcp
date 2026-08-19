"""Crawl-policy reconciler (design D5, spec crawl-control-center).

``reconcile_once(session, launcher, now=None)``:
0. completion probe — close open ``policy_runs`` whose executor job ended;
1. select enabled policies whose cron next-fire <= now (in the policy's
   timezone) AND that have no open run (per-policy single-flight);
2. build the date range from the policy's ``date_policy``
   (since_last -> observation watermarks, trailing -> today - N days,
   explicit -> as given);
3. compile a CrawlPlan via the existing planner (honoring source_filter/mode);
4. guardrail: refuse when the fetch-count estimate exceeds
   ``POLICY_MAX_FETCHES`` (default 50000) unless the policy sets force — the
   refusal is recorded as a failed run;
5. launch the concept-crawl executor through a ``Launcher`` (scrapyd locally,
   k8s Job on the cluster) and record a running ``policy_runs`` row.

Entrypoint: ``python -m fd_open_data_mcp.refresh.reconciler`` (k8s CronJob
``crawl-reconciler``, 15-min cadence, scraw namespace).

The legacy ``schedules`` table is NOT read here — recurring crawls are crawl
policies (spec scheduled-refresh delta).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import subprocess
import urllib.parse
import urllib.request
from typing import Protocol

from croniter import croniter
from sqlalchemy import func
from sqlalchemy.orm import Session
from zoneinfo import ZoneInfo

from fd_open_data_mcp.crawl.plan import CrawlPlan, DateRange, EntityScope
from fd_open_data_mcp.crawl.planner import plan_crawl
from fd_open_data_mcp.models import Cluster, CrawlPolicy, EntitySourceIdentifier, PolicyRun, Proxy

logger = logging.getLogger(__name__)

POLICY_MAX_FETCHES = int(os.environ.get("POLICY_MAX_FETCHES", "50000"))

_OPEN = "running"  # policy_runs.status value for an open run


# ─── launcher abstraction (D5) ───────────────────────────────────────────────
class Launcher(Protocol):
    """Executor backend: launch a compiled plan, probe a launched job.

    ``launch`` returns ``(job_ref, cluster_id)`` where ``cluster_id`` is the
    worker cluster that ran the job (None for scrapyd / single-cluster legacy).
    For multi-cluster, ``job_ref`` encodes the cluster as ``"{name}/{job}"`` so
    ``poll`` can route to the right cluster API without extra state."""

    def launch(self, plan: CrawlPlan, policy: CrawlPolicy) -> tuple[str, int | None]:
        """Start the executor; return (job_ref, cluster_id)."""
        ...

    def poll(self, job_ref: str) -> str:
        """Return 'running' | 'success' | 'failed' | 'unknown' for a job reference."""
        ...


class ScrapydLauncher:
    """Local dev: POST the plan to the shared scrapyd (scraw-ops, :6800).

    The plan JSON is written to ``plan_dir`` (must be readable by the scrapyd
    process/container) and passed to the ``concept_crawl`` spider as its
    ``plan`` kwarg.
    """

    def __init__(
        self,
        scrapyd_url: str | None = None,
        plan_dir: str | None = None,
        project: str = "scraw_fd_open_data_mcp",
        spider: str = "concept_crawl",
    ):
        self.url = (scrapyd_url or os.environ.get("SCRAPYD_URL") or "http://localhost:6800").rstrip("/")
        self.plan_dir = plan_dir or os.environ.get("SCRAW_PLAN_DIR") or "plans"
        self.project = project
        self.spider = spider

    def launch(self, plan: CrawlPlan, policy: CrawlPolicy) -> tuple[str, int | None]:
        os.makedirs(self.plan_dir, exist_ok=True)
        ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = os.path.join(self.plan_dir, f"policy-{policy.id}-{ts}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(plan.model_dump(mode="json"), fh, ensure_ascii=False)
        data = urllib.parse.urlencode({
            "project": self.project, "spider": self.spider, "plan": path,
        }).encode()
        with urllib.request.urlopen(f"{self.url}/schedule.json", data, timeout=30) as r:
            result = json.load(r)
        jobid = result.get("jobid")
        if not jobid:
            raise RuntimeError(f"scrapyd schedule failed: {result}")
        return (jobid, None)

    def poll(self, job_ref: str) -> str:
        try:
            with urllib.request.urlopen(
                f"{self.url}/listjobs.json?project={urllib.parse.quote(self.project)}", timeout=15,
            ) as r:
                jobs = json.load(r)
        except Exception:  # noqa: BLE001 - scrapyd unreachable -> keep the run open
            return "unknown"
        for j in jobs.get("pending", []) + jobs.get("running", []):
            if j.get("id") == job_ref:
                return "running"
        for j in jobs.get("finished", []):
            if j.get("id") == job_ref:
                # scrapyd exposes no per-job outcome; a finished job is a success
                return "success"
        return "unknown"


class K8sJobLauncher:
    """Cluster: one batch/v1 Job per policy run in the scraw namespace.

    Follows the validated crawl-Job pattern (k8s/cn-report-incremental-crawl-job.yaml):
    the plan JSON ships as a ConfigMap mounted at /plan/plan.json and the container
    runs ``scraw-fd-open-data-mcp crawl /plan/plan.json``. Talks to the cluster via
    ``kubectl`` when available (local dev) or the in-cluster service-account REST API
    when running inside the reconciler CronJob pod (no kubectl in the image).
    """

    def __init__(
        self,
        namespace: str | None = None,
        image: str | None = None,
        database_url: str | None = None,
        redis_url: str | None = None,
        kube_context: str | None = None,
    ):
        self.namespace = namespace or os.environ.get("SCRAW_K8S_NAMESPACE", "scraw")
        self.image = image or os.environ.get(
            "SCRAW_K8S_IMAGE", "harbor.local/lawcraw_business/scraw-fd-open-data-mcp:latest")
        self.database_url = database_url or os.environ.get(
            "SCRAW_K8S_DATABASE_URL",
            "postgresql+psycopg2://postgres:admin123@fd-open-pg.scraw:5432/postgres")
        self.redis_url = redis_url or os.environ.get(
            "SCRAW_K8S_REDIS_URL", "redis://fd-open-redis.scraw:6379/0")
        self._ctx = kube_context or os.environ.get("KUBE_CONTEXT")

    # -- transport: kubectl locally, in-cluster REST API inside a pod ---------
    _SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"

    def _in_cluster(self) -> bool:
        import shutil
        return shutil.which("kubectl") is None and os.path.exists(f"{self._SA_DIR}/token")

    def _k8s_api(self, method: str, path: str, body: dict | None = None) -> dict:
        """Minimal in-cluster Kubernetes API call (no kubectl / client lib needed)."""
        import ssl
        with open(f"{self._SA_DIR}/token", encoding="utf-8") as fh:
            token = fh.read().strip()
        ctx = ssl.create_default_context(cafile=f"{self._SA_DIR}/ca.crt")
        req = urllib.request.Request(
            f"https://kubernetes.default.svc{path}", method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, context=ctx, timeout=30) as r:
            return json.load(r)

    def _kubectl(self, *args: str, input_text: str | None = None) -> str:
        cmd = ["kubectl"]
        if self._ctx:
            cmd += ["--context", self._ctx]
        cmd += list(args)
        out = subprocess.run(  # noqa: S603
            cmd, input=input_text, capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            raise RuntimeError(f"kubectl {' '.join(args)} failed: {out.stderr.strip()}")
        return out.stdout

    def launch(self, plan: CrawlPlan, policy: CrawlPolicy) -> tuple[str, int | None]:
        ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M%S")
        name = f"crawl-policy-{policy.id}-{ts}"
        plan_json = json.dumps(plan.model_dump(mode="json"), ensure_ascii=False, indent=2)
        docs = [
            {
                "apiVersion": "v1", "kind": "ConfigMap",
                "metadata": {"name": f"{name}-plan", "namespace": self.namespace,
                             "labels": {"app": "concept-crawl", "policy-id": str(policy.id)}},
                "data": {"plan.json": plan_json},
            },
            {
                "apiVersion": "batch/v1", "kind": "Job",
                "metadata": {"name": name, "namespace": self.namespace,
                             "labels": {"app": "concept-crawl", "policy-id": str(policy.id)}},
                "spec": {
                    "backoffLimit": 2,
                    "ttlSecondsAfterFinished": 86400,
                    "template": {
                        "metadata": {"labels": {"app": "concept-crawl", "policy-id": str(policy.id)}},
                        "spec": {
                            "restartPolicy": "OnFailure",
                            "imagePullSecrets": [{"name": "fd-harbor-pull"}],
                            "containers": [{
                                "name": "crawler",
                                "image": self.image,
                                "imagePullPolicy": "Always",
                                "command": ["scraw-fd-open-data-mcp", "crawl", "/plan/plan.json"],
                                "volumeMounts": [{"name": "plan-volume", "mountPath": "/plan",
                                                  "readOnly": True}],
                                "env": [
                                    {"name": "FD_OPEN_DATA_MCP_DATABASE_URL",
                                     "value": self.database_url},
                                    {"name": "REDIS_URL", "value": self.redis_url},
                                    {"name": "PYTHONUNBUFFERED", "value": "1"},
                                ],
                                "resources": {
                                    "requests": {"memory": "256Mi", "cpu": "200m"},
                                    "limits": {"memory": "1Gi", "cpu": "1000m"},
                                },
                            }],
                            "volumes": [{"name": "plan-volume",
                                         "configMap": {"name": f"{name}-plan"}}],
                        },
                    },
                },
            },
        ]
        if self._in_cluster():
            # names are timestamp-unique -> plain create is enough (no apply needed)
            self._k8s_api("POST", f"/api/v1/namespaces/{self.namespace}/configmaps", docs[0])
            self._k8s_api("POST", f"/apis/batch/v1/namespaces/{self.namespace}/jobs", docs[1])
        else:
            self._kubectl("apply", "-f", "-",
                          input_text="\n---\n".join(json.dumps(d) for d in docs))
        return (name, None)

    def poll(self, job_ref: str) -> str:
        if self._in_cluster():
            try:
                st = self._k8s_api(
                    "GET", f"/apis/batch/v1/namespaces/{self.namespace}/jobs/{job_ref}"
                ).get("status", {})
            except Exception:  # noqa: BLE001 - api unreachable / job gone
                return "unknown"
            if st.get("succeeded"):
                return "success"
            if st.get("failed"):
                return "failed"
            if st.get("active"):
                return "running"
            return "unknown"
        try:
            out = self._kubectl(
                "get", "job", job_ref, "-n", self.namespace,
                "-o", "jsonpath={.status.active} {.status.succeeded} {.status.failed}")
        except Exception:  # noqa: BLE001 - api unreachable / job gone
            return "unknown"
        active, succeeded, failed = (out.split() + ["", "", ""])[:3]
        if succeeded and succeeded != "0":
            return "success"
        if failed and failed != "0":
            return "failed"
        if active and active != "0":
            return "running"
        return "unknown"


# ─── multi-cluster dispatch (add-multi-cluster-master-db) ────────────────────
# A fleet of worker clusters (cloud servers) share one master Postgres + Redis.
# Each cluster has its own egress IP -> distinct `direct` proxy -> independent
# circuit breaker. Adding a server = insert a `clusters` row + mount its
# kubeconfig Secret; ClusterScheduler picks one per plan (tags cover the plan's
# sources, egress not circuit-open, fewest open runs, < capacity).

class ClusterK8sClient:
    """K8s API client for ONE worker cluster: api_server (from the DB row) + a
    bearer token + optional CA, read as flat files from the single mounted Secret
    ``$KUBECONFIG_DIR/<name>.{token,ca}`` (operator drops each cluster's creds as
    keys in one Secret - adding a server adds 2 Secret keys + a DB row, no manifest
    edit). Reuses the in-cluster REST pattern of K8sJobLauncher but per-cluster."""

    def __init__(self, cluster: Cluster):
        self.cluster = cluster
        self._creds: tuple[str, str, str | None] | None = None  # (api_server, token, ca_path)

    def _load(self) -> tuple[str, str, str | None]:
        if self._creds is None:
            base = os.environ.get("KUBECONFIG_DIR", "/kubeconfigs")
            name = self.cluster.name
            with open(os.path.join(base, f"{name}.token"), encoding="utf-8") as f:
                token = f.read().strip()
            ca_path = os.path.join(base, f"{name}.ca")
            ca_path = ca_path if os.path.exists(ca_path) else None  # CA optional
            self._creds = (self.cluster.api_server, token, ca_path)
        return self._creds

    def _api(self, method: str, path: str, body: dict | None = None) -> dict:
        import ssl
        api_server, token, ca = self._load()
        ctx = ssl.create_default_context(cafile=ca) if ca else ssl.create_default_context()
        req = urllib.request.Request(
            f"{api_server}{path}", method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, context=ctx, timeout=30) as r:
            return json.load(r)

    def create(self, manifest: dict) -> dict:
        kind = manifest["kind"]
        ns = manifest["metadata"]["namespace"]
        if kind == "ConfigMap":
            return self._api("POST", f"/api/v1/namespaces/{ns}/configmaps", manifest)
        if kind == "Job":
            return self._api("POST", f"/apis/batch/v1/namespaces/{ns}/jobs", manifest)
        raise ValueError(f"ClusterK8sClient.create: unsupported kind {kind}")

    def job_status(self, name: str) -> str:
        ns = self.cluster.namespace
        st = self._api("GET", f"/apis/batch/v1/namespaces/{ns}/jobs/{name}").get("status", {})
        if st.get("succeeded"):
            return "success"
        if st.get("failed"):
            return "failed"
        if st.get("active"):
            return "running"
        return "unknown"


def pick_cluster(session: Session, plan: CrawlPlan) -> Cluster | None:
    """ClusterScheduler: choose a worker cluster for a plan.

    Filters: enabled; tags cover the plan's real_sources (empty tags = wildcard,
    fetch anything); open runs < capacity; egress not circuit-open for any
    required source. Ranks by fewest open runs (load balance). None if no
    eligible cluster (the reconciler records the run as failed)."""
    from fd_open_data_mcp.proxy.circuit import is_selectable

    sources = {rs.source for pc in plan.wanted_concepts for rs in pc.ranked_sources}
    candidates = session.query(Cluster).filter_by(enabled=True).all()
    if not candidates:
        return None
    eligible: list[tuple[Cluster, int]] = []
    for c in candidates:
        tags = set(c.tags or [])
        if tags and not sources.issubset(tags):
            continue  # cluster can't fetch some required source
        open_runs = (session.query(PolicyRun)
                     .filter_by(cluster_id=c.id, status=_OPEN).count())
        if open_runs >= c.capacity:
            continue  # at capacity
        direct = (session.query(Proxy)
                  .filter_by(scheme="direct", cluster_id=c.id).first())
        if direct is not None and direct.id is not None:
            if not all(is_selectable(src, direct.id) for src in sources):
                continue  # this cluster's egress is banned for a required source
        eligible.append((c, open_runs))
    if not eligible:
        return None
    eligible.sort(key=lambda x: x[1])
    return eligible[0][0]


class MultiClusterLauncher:
    """Dispatch crawl Jobs across the worker-cluster fleet.

    ``launch`` picks a cluster (ClusterScheduler), creates a ConfigMap + Job
    there via ClusterK8sClient, and returns ``("{cluster_name}/{job_name}",
    cluster.id)``. ``poll`` parses the cluster prefix and probes that cluster's
    Job. Workers point at the shared master PG (PgBouncer) + master Redis, and
    self-register their egress (concept_crawl_spider._register_egress); the
    ProxySelector pins to that egress so the circuit tracks per-cluster."""

    def __init__(self, database_url: str | None = None, redis_url: str | None = None):
        self.database_url = database_url or os.environ.get(
            "SCRAW_K8S_DATABASE_URL",
            "postgresql+psycopg2://postgres:admin123@fd-open-pg.scraw:5432/postgres")
        self.redis_url = redis_url or os.environ.get(
            "SCRAW_K8S_REDIS_URL", "redis://fd-open-redis.scraw:6379/0")

    def _session(self) -> Session:
        from fd_open_data_mcp.db import get_database
        return get_database().get_session()

    def launch(self, plan: CrawlPlan, policy: CrawlPolicy) -> tuple[str, int | None]:
        session = self._session()
        try:
            cluster = pick_cluster(session, plan)
            if cluster is None:
                raise RuntimeError(
                    "no eligible worker cluster for plan (check clusters table: "
                    "enabled+tags, circuit state, capacity)")
        finally:
            session.close()
        ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M%S")
        name = f"crawl-policy-{policy.id}-{ts}"
        plan_json = json.dumps(plan.model_dump(mode="json"), ensure_ascii=False, indent=2)
        docs = [
            {
                "apiVersion": "v1", "kind": "ConfigMap",
                "metadata": {"name": f"{name}-plan", "namespace": cluster.namespace,
                             "labels": {"app": "concept-crawl", "policy-id": str(policy.id),
                                        "cluster": cluster.name}},
                "data": {"plan.json": plan_json},
            },
            {
                "apiVersion": "batch/v1", "kind": "Job",
                "metadata": {"name": name, "namespace": cluster.namespace,
                             "labels": {"app": "concept-crawl", "policy-id": str(policy.id),
                                        "cluster": cluster.name}},
                "spec": {
                    "backoffLimit": 2,
                    "ttlSecondsAfterFinished": 86400,
                    "template": {
                        "metadata": {"labels": {"app": "concept-crawl",
                                                "policy-id": str(policy.id),
                                                "cluster": cluster.name}},
                        "spec": {
                            "restartPolicy": "OnFailure",
                            "imagePullSecrets": [{"name": "fd-harbor-pull"}],
                            "containers": [{
                                "name": "crawler",
                                "image": cluster.image,
                                "imagePullPolicy": "Always",
                                "command": ["scraw-fd-open-data-mcp", "crawl", "/plan/plan.json"],
                                "volumeMounts": [{"name": "plan-volume", "mountPath": "/plan",
                                                  "readOnly": True}],
                                "env": [
                                    {"name": "FD_OPEN_DATA_MCP_DATABASE_URL",
                                     "value": self.database_url},
                                    {"name": "REDIS_URL", "value": self.redis_url},
                                    # tells the worker which cluster it is so the
                                    # ProxySelector pins to its own egress (per-cluster circuit)
                                    {"name": "SCRAW_CLUSTER_ID", "value": str(cluster.id)},
                                    {"name": "SCRAW_CLUSTER_NAME", "value": cluster.name},
                                    # Decoupled egress pool (LEGACY — no-op once proxy-fw is
                                    # the sole path; kept during the overlap so a worker
                                    # whose proxy-fw is not yet running still opts into the
                                    # old gost scheme='http' pool). ``pool`` skips the
                                    # SCRAW_CLUSTER_ID pinning branch in selector.py; on keeps
                                    # the pool active. Both default to the decoupled values so
                                    # a missing reconciler env still opts workers into the pool.
                                    {"name": "FD_EGRESS_MODE",
                                     "value": os.environ.get("FD_EGRESS_MODE", "pool")},
                                    {"name": "FD_PROXY_POOL",
                                     "value": os.environ.get("FD_PROXY_POOL", "on")},
                                    # Standalone forwarder: the worker calls
                                    # ``proxy_client.acquire(source)`` per fetch,
                                    # the forwarder returns an upstream_url
                                    # (gost-own on this cluster's node), and the
                                    # worker injects it into its own HTTP client
                                    # (terminating TLS itself to classify bans).
                                    # The hostNetwork forwarder binds the node
                                    # IP:8080 (no Service possible under the
                                    # crawl-worker SA) and worker clusters are
                                    # single-node, so the pod's OWN node IP —
                                    # downward API ``status.hostIP`` — is the
                                    # forwarder's address. K8S_NODE_IP must be
                                    # declared BEFORE FD_PROXY_FORWARDER: k8s
                                    # only expands ``$(VAR)`` refs to earlier
                                    # env entries.
                                    {"name": "K8S_NODE_IP",
                                     "valueFrom": {"fieldRef": {
                                         "fieldPath": "status.hostIP"}}},
                                    {"name": "FD_PROXY_FORWARDER",
                                     "value": "http://$(K8S_NODE_IP):8080"},
                                    {"name": "PYTHONUNBUFFERED", "value": "1"},
                                ],
                                "resources": {
                                    "requests": {"memory": "256Mi", "cpu": "200m"},
                                    "limits": {"memory": "1Gi", "cpu": "1000m"},
                                },
                            }],
                            "volumes": [{"name": "plan-volume",
                                         "configMap": {"name": f"{name}-plan"}}],
                        },
                    },
                },
            },
        ]
        client = ClusterK8sClient(cluster)
        client.create(docs[0])
        client.create(docs[1])
        logger.info("launched policy %s on cluster %s as job=%s", policy.name, cluster.name, name)
        return (f"{cluster.name}/{name}", cluster.id)

    def poll(self, job_ref: str) -> str:
        if "/" not in job_ref:
            return "unknown"
        cluster_name, job_name = job_ref.split("/", 1)
        session = self._session()
        try:
            cluster = session.query(Cluster).filter_by(name=cluster_name).first()
        finally:
            session.close()
        if cluster is None:
            return "unknown"
        try:
            return ClusterK8sClient(cluster).job_status(job_name)
        except Exception:  # noqa: BLE001 - api unreachable / job gone
            return "unknown"


# ─── date-range builder (5.2) ────────────────────────────────────────────────
def build_date_range(policy: CrawlPolicy, today: dt.date) -> tuple[DateRange, bool]:
    """Compile the policy's date_policy into a DateRange + since_last flag.

    - since_last: start=None (the planner derives it from observation
      watermarks), end=today
    - trailing:   start=today - days, end=today
    - explicit:   start/end as given (end defaults to today)
    """
    dp = policy.date_policy or {}
    mode = dp.get("mode", "since_last")
    if mode == "trailing":
        days = int(dp.get("days", 1))
        return DateRange(start=(today - dt.timedelta(days=days)).isoformat(),
                         end=today.isoformat(), frequency=policy.frequency), False
    if mode == "explicit":
        return DateRange(start=dp.get("start"), end=dp.get("end") or today.isoformat(),
                         frequency=policy.frequency), False
    # since_last (default)
    return DateRange(start=None, end=today.isoformat(),
                     frequency=policy.frequency), True


# ─── plan-size estimate (5.3) ────────────────────────────────────────────────
def _date_count(start: str, end: str, frequency: str | None) -> int:
    """Number of fetch dates in [start, end] for a cadence (mirrors the spider's
    ``_expand_dates``): yearly -> one per year, monthly -> one per month, else daily."""
    s = dt.date.fromisoformat(start[:10])
    e = dt.date.fromisoformat(end[:10])
    if e < s:
        s, e = e, s
    if frequency == "yearly":
        return e.year - s.year + 1
    if frequency == "monthly":
        return (e.year - s.year) * 12 + (e.month - s.month) + 1
    return (e - s).days + 1


def estimate_fetches(session: Session, plan: CrawlPlan) -> int:
    """Estimated fetch count: sum over concepts of entities x dates
    (series mode: one fetch per (concept, entity), design D6)."""
    scope = plan.entity_scope
    if scope.entity_ids:
        n_entities = len(scope.entity_ids)
    else:
        # entities of the type carrying an identifier for at least one ranked source
        sources = {rs.source for pc in plan.wanted_concepts for rs in pc.ranked_sources}
        if sources:
            n_entities = (
                session.query(func.count(func.distinct(EntitySourceIdentifier.entity_id)))
                .filter(EntitySourceIdentifier.entity_type == scope.entity_type,
                        EntitySourceIdentifier.source.in_(sources))
                .scalar()
            ) or 0
        else:
            n_entities = 0   # no ranked sources -> nothing fetchable
    total = 0
    for pc in plan.wanted_concepts:
        if plan.mode == "series" or plan.date_range.start is None:
            n_dates = 1
        else:
            n_dates = _date_count(plan.date_range.start, plan.date_range.end,
                                  pc.frequency or plan.date_range.frequency)
        total += n_entities * n_dates
    return total


# ─── due-policy selection (5.1) ──────────────────────────────────────────────
def _policy_tz(policy: CrawlPolicy):
    """The policy's configured timezone (cron due-ness AND crawl 'today' both use it —
    hoisted so the crawl date range matches the calendar day the cron fired on)."""
    return ZoneInfo(policy.timezone or "UTC")


def _cron_due(policy: CrawlPolicy, now: dt.datetime) -> bool:
    """True when the cron's next fire after the last run (or creation) is <= now,
    computed in the policy's timezone."""
    tz = _policy_tz(policy)
    local_now = now.astimezone(tz)
    base = (policy.last_run_at or policy.created_at)
    if base is None:
        return True
    if base.tzinfo is None:
        base = base.replace(tzinfo=dt.timezone.utc)
    next_fire = croniter(policy.cron_expr, base.astimezone(tz)).get_next(dt.datetime)
    return next_fire <= local_now


def _open_run(session: Session, policy_id: int) -> PolicyRun | None:
    return (session.query(PolicyRun)
            .filter_by(policy_id=policy_id, status=_OPEN)
            .first())


# ─── single-policy launch (shared by reconciler tick and MCP trigger-now) ───
def launch_policy(
    session: Session,
    policy: CrawlPolicy,
    launcher: Launcher,
    now: dt.datetime | None = None,
) -> dict:
    """Single-flight + plan + guardrail + launch for one policy (design D5 steps 1-5).

    Does NOT check the cron schedule — the reconciler checks due-ness before
    calling; the MCP trigger-now tool bypasses cron but still goes through the
    single-flight and plan-size guardrails here (spec crawl-control-center).

    Returns {"policy": name, "status": launched|skipped|refused, ...}.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)

    if _open_run(session, policy.id) is not None:
        logger.info("policy %s due but has an open run; skipping (single-flight)", policy.name)
        return {"policy": policy.name, "status": "skipped", "reason": "open run (single-flight)"}

    # the crawl window uses the policy's LOCAL calendar day, not UTC's — at 01:00
    # Beijing (17:00 UTC prev day) a trailing/since_last policy must cover Beijing
    # today, not UTC yesterday (fix-observation-time-granularity, spec crawl-control-center)
    local_today = now.astimezone(_policy_tz(policy)).date()
    date_range, since_last = build_date_range(policy, local_today)
    plan = plan_crawl(
        session, list(policy.concept_ids or []),
        EntityScope(entity_type=policy.entity_type,
                    entity_ids=policy.entity_ids),
        date_range, since_last=since_last,
        source_filter=policy.source_filter, mode=policy.mode or "per_date",
    )

    # guardrail: plan-size estimate vs POLICY_MAX_FETCHES (spec crawl-control-center)
    estimate = estimate_fetches(session, plan)
    if estimate > POLICY_MAX_FETCHES and not policy.force:
        detail = (f"refused: estimated {estimate} fetches exceeds "
                  f"POLICY_MAX_FETCHES={POLICY_MAX_FETCHES} (set force=true to override)")
        logger.warning("policy %s %s", policy.name, detail)
        session.add(PolicyRun(policy_id=policy.id, status="failed",
                              plan_json=plan.model_dump(mode="json"),
                              started_at=now, finished_at=now, detail=detail))
        policy.last_run_at = now
        session.commit()
        return {"policy": policy.name, "status": "refused", "estimate": estimate,
                "reason": detail}

    try:
        job_ref, cluster_id = launcher.launch(plan, policy)
    except Exception as e:  # noqa: BLE001 - a broken launcher must not kill the tick
        logger.exception("policy %s launch failed", policy.name)
        session.add(PolicyRun(policy_id=policy.id, status="failed",
                              plan_json=plan.model_dump(mode="json"),
                              started_at=now, finished_at=now,
                              detail=f"launch failed: {e}"))
        policy.last_run_at = now
        session.commit()
        return {"policy": policy.name, "status": "refused", "reason": f"launch failed: {e}"}

    session.add(PolicyRun(policy_id=policy.id, status=_OPEN,
                          plan_json=plan.model_dump(mode="json"),
                          job_ref=job_ref, cluster_id=cluster_id, started_at=now))
    policy.last_run_at = now
    session.commit()
    logger.info("policy %s launched job=%s estimate=%d", policy.name, job_ref, estimate)
    return {"policy": policy.name, "status": "launched", "job_ref": job_ref,
            "estimate": estimate}


# ─── the reconciler ──────────────────────────────────────────────────────────
def reconcile_once(
    session: Session,
    launcher: Launcher,
    now: dt.datetime | None = None,
) -> dict:
    """One reconciler tick: probe open runs, launch due policies. Returns a summary."""
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    summary = {"probed_closed": 0, "launched": [], "skipped": [], "refused": []}

    # 0. completion probe: close open runs whose executor job ended (D5 step 5)
    for run in session.query(PolicyRun).filter_by(status=_OPEN).all():
        if not run.job_ref:
            continue
        state = launcher.poll(run.job_ref)
        if state in ("success", "failed"):
            run.status = state
            run.finished_at = now
            summary["probed_closed"] += 1
    session.commit()

    # 1-5. due policies
    for policy in session.query(CrawlPolicy).filter_by(enabled=True).all():
        if not _cron_due(policy, now):
            continue
        result = launch_policy(session, policy, launcher, now)
        status = result.pop("status")
        summary[{"launched": "launched", "skipped": "skipped",
                 "refused": "refused"}[status]].append(result)

    return summary


def _default_launcher() -> Launcher:
    kind = os.environ.get("RECONCILER_LAUNCHER", "scrapyd")
    if kind == "k8s":
        return K8sJobLauncher()
    if kind == "k8s-multi":
        return MultiClusterLauncher()
    return ScrapydLauncher()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from fd_open_data_mcp.db import get_database

    session = get_database().get_session()
    try:
        summary = reconcile_once(session, _default_launcher())
    finally:
        session.close()
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
