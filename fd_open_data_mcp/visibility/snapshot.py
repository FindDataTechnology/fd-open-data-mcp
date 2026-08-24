"""Shared snapshot builder for the crawl watcher + ``crawl_status`` tool
(add-crawl-visibility).

One set of read-only queries over the control-plane tables, used by BOTH:

- ``visibility.digest`` (formats the snapshot into a WeChat message), and
- the ``crawl_status`` MCP tool (returns it as structured JSON).

So the on-demand "ask Claude what the scraw is doing" answer and the daily
digest's projection are guaranteed identical (spec crawl-control-center:
"both the tool and the digest share one snapshot-building function").

Every function takes a SQLAlchemy session (the same session factory the other
control-plane tools use) and returns plain JSON-serializable data. Nothing
here mutates any table.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Optional

from croniter import croniter
from sqlalchemy import func
from sqlalchemy.orm import Session
from zoneinfo import ZoneInfo

from fd_open_data_mcp.models import (
    CrawlPolicy, Cluster, EntitySourceIdentifier, FetchLog, PolicyRun,
    SourceProxyHealth,
)
from fd_open_data_mcp.visibility import state as _state

logger = logging.getLogger(__name__)

_STALE_MIN = int(__import__("os").environ.get("SCRAW_STALE_MINUTES", "90"))
_DIGEST_TZ = __import__("os").environ.get("SCRAW_DIGEST_TZ", "Asia/Shanghai")


def _iso(v: dt.datetime | None) -> str | None:
    return v.isoformat() if v else None


def _as_aware_utc(v: dt.datetime | None) -> dt.datetime | None:
    """Normalize a datetime to aware UTC.

    The control-plane tables use ``TIMESTAMP WITHOUT TIME ZONE`` and the
    reconciler writes naive-UTC, so datetimes read back from the DB are naive.
    Assume naive == UTC (the documented writer contract) rather than local
    time, so age/cutoff math is correct on any host timezone.
    """
    if v is None:
        return None
    if v.tzinfo is None:
        return v.replace(tzinfo=dt.timezone.utc)
    return v.astimezone(dt.timezone.utc)


def _policy_tz(policy: CrawlPolicy) -> ZoneInfo:
    return ZoneInfo(policy.timezone or "UTC")


# --- recent runs -------------------------------------------------------------
def recent_runs(session: Session, limit: int = 20) -> list[dict]:
    """Latest ``policy_runs`` rows with policy name + the target datasource chain."""
    rows = (
        session.query(PolicyRun, CrawlPolicy.name)
        .join(CrawlPolicy, PolicyRun.policy_id == CrawlPolicy.id)
        .order_by(PolicyRun.started_at.desc())
        .limit(limit)
        .all()
    )
    out = []
    for run, pname in rows:
        out.append({
            "id": run.id, "policy_id": run.policy_id, "policy": pname,
            "status": run.status, "cluster_id": run.cluster_id,
            "job_ref": run.job_ref,
            "started_at": _iso(run.started_at), "finished_at": _iso(run.finished_at),
            "detail": run.detail,
            "datasources": _plan_datasources(run.plan_json),
        })
    return out


def _plan_datasources(plan_json: dict | None) -> list[str]:
    """The target source names a run's plan would hit (from its ranked_sources).

    ``plan_json`` is the compiled CrawlPlan stored on the run row; we read the
    ranked ``source`` field directly so the scan/digest never re-compiles a
    plan just to label a past run.
    """
    if not plan_json or not isinstance(plan_json, dict):
        return []
    wanted = plan_json.get("wanted_concepts") or []
    sources: set[str] = set()
    for pc in wanted:
        for rs in pc.get("ranked_sources") or []:
            src = rs.get("source")
            if src:
                sources.add(src)
    return sorted(sources)


# --- fleet health ------------------------------------------------------------
def fleet_health(session: Session) -> list[dict]:
    """Each ``clusters`` row + open-run count vs capacity + API reachability."""
    clusters = session.query(Cluster).order_by(Cluster.id).all()
    out = []
    for c in clusters:
        open_runs = (
            session.query(func.count(PolicyRun.id))
            .filter_by(cluster_id=c.id, status="running")
            .scalar()
        ) or 0
        reachable = _probe_cluster(c) if c.enabled else None
        out.append({
            "id": c.id, "name": c.name, "enabled": c.enabled,
            "namespace": c.namespace, "capacity": c.capacity,
            "open_runs": open_runs, "reachable": reachable,
            "tags": c.tags or [],
            "api_server": c.api_server,
        })
    return out


def _probe_cluster(cluster: Cluster) -> bool:
    """Lightweight reachability probe: a trivial k8s API GET, 10s timeout.

    Reuses ``ClusterK8sClient``'s transport (bearer token + CA from the mounted
    Secret). Any HTTP response (even a 404/403) means the API is up; only a
    connection/timeout failure marks the cluster unreachable.
    """
    try:
        from fd_open_data_mcp.refresh.reconciler import ClusterK8sClient

        client = ClusterK8sClient(cluster)
        # GET the batch API group root — always present, cheap. A 404 here would
        # raise inside _api; we treat *any* successful HTTP exchange as reachable.
        client._api("GET", "/apis/batch/v1")
        return True
    except Exception as e:  # noqa: BLE001 - unreachable/timeout/bad creds
        logger.debug("cluster %s probe failed: %s", cluster.name, e)
        return False


# --- stale runs --------------------------------------------------------------
def stale_runs(session: Session, stale_min: int = _STALE_MIN) -> list[dict]:
    """``policy_runs`` still ``running`` past the stale threshold."""
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=stale_min)
    # The DB column is TIMESTAMP WITHOUT TIME ZONE (naive-UTC per the writer
    # contract), so filter against a naive cutoff to avoid a naive/aware
    # mismatch on Postgres; re-attach tz for the in-Python age math below.
    cutoff_naive = cutoff.replace(tzinfo=None)
    rows = (
        session.query(PolicyRun, CrawlPolicy.name)
        .join(CrawlPolicy, PolicyRun.policy_id == CrawlPolicy.id)
        .filter(PolicyRun.status == "running", PolicyRun.started_at < cutoff_naive)
        .order_by(PolicyRun.started_at.asc())
        .all()
    )
    out = []
    now_utc = dt.datetime.now(dt.timezone.utc)
    for run, pname in rows:
        started = _as_aware_utc(run.started_at) or now_utc
        age_min = int((now_utc - started).total_seconds() // 60)
        out.append({
            "id": run.id, "policy_id": run.policy_id, "policy": pname,
            "cluster_id": run.cluster_id, "job_ref": run.job_ref,
            "started_at": _iso(run.started_at), "age_minutes": age_min,
            "datasources": _plan_datasources(run.plan_json),
        })
    return out


# --- per-source fetch outcome ------------------------------------------------
def per_source_outcome(session: Session, hours: int = 24) -> list[dict]:
    """Per-``real_source`` ok/error counts from ``fetch_log`` over the window.

    ``real_source`` is the true upstream (eastmoney, wbgapi, …); rows with no
    real_source (untagged adapter calls) are bucketed under ``(untracked)`` so
    they don't disappear from the digest but stay distinguishable.
    """
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    since_naive = since.replace(tzinfo=None)  # naive-UTC column, see stale_runs
    rows = (
        session.query(FetchLog.real_source, FetchLog.status, func.count(FetchLog.id))
        .filter(FetchLog.timestamp > since_naive)
        .group_by(FetchLog.real_source, FetchLog.status)
        .all()
    )
    by_src: dict[str, dict] = {}
    for real_source, status, cnt in rows:
        key = real_source or "(untracked)"
        bucket = by_src.setdefault(key, {"datasource": key, "ok": 0, "err": 0})
        if status == "ok":
            bucket["ok"] += cnt
        else:
            bucket["err"] += cnt
    return sorted(by_src.values(), key=lambda b: (b["ok"] + b["err"]), reverse=True)


# --- circuit state -----------------------------------------------------------
def circuit_state(session: Session) -> list[dict]:
    """Per-source circuit health from ``source_proxy_health`` + the Redis hot state."""
    rows = (
        session.query(SourceProxyHealth)
        .order_by(SourceProxyHealth.source)
        .all()
    )
    out = []
    for h in rows:
        out.append({
            "source": h.source, "proxy_id": h.proxy_id,
            "state": h.state, "permanent": h.permanent,
            "fail_streak": h.fail_streak, "open_cycles": h.open_cycles,
            "cooldown_until": _iso(h.cooldown_until),
            "last_success_at": _iso(h.last_success_at),
        })
    return out


# --- today's scheduled → target datasources ----------------------------------
def today_scheduled(session: Session, tz: str = _DIGEST_TZ) -> list[dict]:
    """Enabled policies whose next cron fire lands today, projected to target sources.

    For each enabled ``crawl_policies`` row: compute the next fire after
    ``last_run_at`` (or creation) via ``croniter`` in the policy's timezone; if
    that fire's calendar day equals today (in the digest tz), compile a plan
    (``plan_crawl``) and collect the target ``source``(s) from its
    ``ranked_sources`` + the entity count from the policy's scope. The
    projection is cached in Redis for the day so the digest compiles plans once.
    """
    from fd_open_data_mcp.crawl.plan import DateRange, EntityScope
    from fd_open_data_mcp.crawl.planner import plan_crawl
    from fd_open_data_mcp.refresh.reconciler import build_date_range, estimate_fetches

    digest_tz = ZoneInfo(tz)
    local_today = dt.datetime.now(digest_tz).date()
    cache_key = f"crawl_watcher:digest:{local_today.isoformat()}"
    r = _state._client()
    if r is not None:
        cached = r.get(cache_key)
        if cached:
            try:
                return json.loads(cached)
            except (TypeError, ValueError):
                pass  # corrupt cache → recompute

    policies = session.query(CrawlPolicy).filter_by(enabled=True).all()
    out = []
    now = dt.datetime.now(dt.timezone.utc)
    for p in policies:
        try:
            ptz = _policy_tz(p)
            base = _as_aware_utc(p.last_run_at) or _as_aware_utc(p.created_at)
            if base is None:
                next_fire = croniter(p.cron_expr, now.astimezone(ptz)).get_next(dt.datetime)
            else:
                next_fire = croniter(p.cron_expr, base.astimezone(ptz)).get_next(dt.datetime)
        except Exception as e:  # noqa: BLE001 - a bad cron must not break the digest
            logger.warning("today_scheduled: policy %s cron parse failed: %s", p.name, e)
            continue
        if next_fire.astimezone(digest_tz).date() != local_today:
            continue
        # compile the plan to resolve target sources + entity count
        try:
            local_today_for_plan = next_fire.astimezone(ptz).date()
            date_range, since_last = build_date_range(p, local_today_for_plan)
            plan = plan_crawl(
                session, list(p.concept_ids or []),
                EntityScope(entity_type=p.entity_type, entity_ids=p.entity_ids),
                date_range, since_last=since_last,
                source_filter=p.source_filter, mode=p.mode or "per_date",
            )
            sources = sorted({rs.source for pc in plan.wanted_concepts for rs in pc.ranked_sources})
            n_entities = _entity_count(session, p, sources)
            out.append({
                "policy_id": p.id, "policy": p.name,
                "next_fire": next_fire.isoformat(),
                "datasources": sources,
                "entities": n_entities,
                "unroutable": len(plan.unroutable),
            })
        except Exception as e:  # noqa: BLE001 - one policy's plan failure skips just it
            logger.warning("today_scheduled: policy %s plan failed: %s", p.name, e)
            out.append({
                "policy_id": p.id, "policy": p.name,
                "next_fire": next_fire.isoformat(),
                "datasources": [], "entities": None,
                "unroutable": None, "error": str(e),
            })

    out.sort(key=lambda x: x.get("next_fire") or "")
    if r is not None:
        try:
            r.set(cache_key, json.dumps(out, ensure_ascii=False), ex=6 * 3600)
        except Exception as e:  # noqa: BLE001
            logger.debug("today_scheduled cache write failed: %s", e)
    return out


def _entity_count(session: Session, policy: CrawlPolicy, sources: list[str]) -> int | None:
    """Entity count for a policy scope: explicit list length, or the count of
    entities of the type carrying an identifier for at least one ranked source
    (mirrors ``estimate_fetches``; ``None`` if unknowable)."""
    if policy.entity_ids:
        return len(policy.entity_ids)
    if not sources:
        return 0
    n = (
        session.query(func.count(func.distinct(EntitySourceIdentifier.entity_id)))
        .filter(EntitySourceIdentifier.entity_type == policy.entity_type,
                EntitySourceIdentifier.source.in_(sources))
        .scalar()
    ) or 0
    return n


# --- the composite snapshot --------------------------------------------------
def build_snapshot(session: Session, *, hours: int = 24, run_limit: int = 20) -> dict:
    """Full datasource-centric snapshot — the one call the digest + tool share."""
    runs = recent_runs(session, limit=run_limit)
    fleet = fleet_health(session)
    stale = stale_runs(session)
    sources = per_source_outcome(session, hours=hours)
    circuits = circuit_state(session)
    scheduled = today_scheduled(session)
    # roll-up counts
    n_ok = sum(r["ok"] for r in sources)
    n_err = sum(r["err"] for r in sources)
    return {
        "generated_at": _iso(dt.datetime.now(dt.timezone.utc)),
        "window_hours": hours,
        "recent_runs": runs,
        "fleet": fleet,
        "stale_runs": stale,
        "per_source_outcome": sources,
        "circuit_state": circuits,
        "today_scheduled": scheduled,
        "summary": {
            "fetches_ok_24h": n_ok,
            "fetches_err_24h": n_err,
            "stale_run_count": len(stale),
            "fleet_enabled": sum(1 for f in fleet if f["enabled"]),
            "fleet_unreachable": [f["name"] for f in fleet if f["enabled"] and f["reachable"] is False],
        },
    }
