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
from fd_open_data_mcp.models import CrawlPolicy, EntitySourceIdentifier, PolicyRun

logger = logging.getLogger(__name__)

POLICY_MAX_FETCHES = int(os.environ.get("POLICY_MAX_FETCHES", "50000"))

_OPEN = "running"  # policy_runs.status value for an open run


# ─── launcher abstraction (D5) ───────────────────────────────────────────────
class Launcher(Protocol):
    """Executor backend: launch a compiled plan, probe a launched job."""

    def launch(self, plan: CrawlPlan, policy: CrawlPolicy) -> str:
        """Start the executor; return a job reference (scrapyd jobid / k8s Job name)."""
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

    def launch(self, plan: CrawlPlan, policy: CrawlPolicy) -> str:
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
        return jobid

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

    def launch(self, plan: CrawlPlan, policy: CrawlPolicy) -> str:
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
        return name

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
        n_entities = (
            session.query(func.count(func.distinct(EntitySourceIdentifier.entity_id)))
            .filter(EntitySourceIdentifier.entity_type == scope.entity_type,
                    EntitySourceIdentifier.source.in_(sources or {"\x00"}))
            .scalar()
        ) or 0
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
def _cron_due(policy: CrawlPolicy, now: dt.datetime) -> bool:
    """True when the cron's next fire after the last run (or creation) is <= now,
    computed in the policy's timezone."""
    tz = ZoneInfo(policy.timezone or "UTC")
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

    date_range, since_last = build_date_range(policy, now.date())
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
        job_ref = launcher.launch(plan, policy)
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
                          job_ref=job_ref, started_at=now))
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
