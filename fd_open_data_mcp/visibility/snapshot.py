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
from sqlalchemy import and_, func
from sqlalchemy.orm import Session
from zoneinfo import ZoneInfo

from fd_open_data_mcp.models import (
    Analysis, Candidate, CrawlIdentity, CrawlIdentityEvent, CrawlPolicy,
    CrawlRun, CrawlSite, CrawlSource, Discovery, PendingRun, Cluster,
    EntitySourceIdentifier, FetchLog, PolicyRun, SourceProxyHealth,
    SourceManifest,
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
            # recorded yield (fix-silent-zero-yield-crawls): absent counters read
            # as null — which is itself the "pod never reported" signal
            "plan_cells": run.plan_cells,
            "rows_attempted": run.rows_attempted,
            "rows_new": run.rows_new,
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
def _job_status(cluster: Cluster, job_ref: str) -> str:
    """Live executor-Job status for an OPEN run (panel-ops-console 4.3):
    'running' | 'success' | 'failed' | 'unknown'. Best-effort — an unreachable
    cluster API or a legacy scrapyd ref degrades to 'unknown'."""
    if not job_ref or "/" not in job_ref:
        return "unknown"
    try:
        from fd_open_data_mcp.refresh.reconciler import ClusterK8sClient

        st = ClusterK8sClient(cluster).job_status(job_ref.split("/", 1)[1])
        return st if st in ("running", "success", "failed") else "unknown"
    except Exception:  # noqa: BLE001 - probe must never break the fleet view
        return "unknown"


def fleet_health(session: Session) -> list[dict]:
    """Each ``clusters`` row + open-run count vs capacity + API reachability +
    a per-cluster egress summary (panel-ops-console): sources whose circuit is
    OPEN/permanent on the cluster's own direct-egress proxy. Open runs are
    additionally bucketed by LIVE executor-Job state so a crashed Job is
    visible while its run row is still open."""
    from fd_open_data_mcp.models import Proxy, SourceProxyHealth

    clusters = session.query(Cluster).order_by(Cluster.id).all()
    out = []
    for c in clusters:
        open_runs_rows = (
            session.query(PolicyRun.job_ref)
            .filter_by(cluster_id=c.id, status="running")
            .all()
        )
        open_runs = len(open_runs_rows)
        reachable = _probe_cluster(c) if c.enabled else None
        # live Job buckets for the cluster's open runs (best-effort probes)
        job_states = {"running": 0, "success": 0, "failed": 0, "unknown": 0}
        for (ref,) in open_runs_rows:
            job_states[_job_status(c, ref)] += 1
        # egress summary: the cluster's own `direct` proxy row mirrors
        # pick_cluster's lookup; a source is "banned" when state=open or permanent
        direct = session.query(Proxy).filter_by(scheme="direct", cluster_id=c.id).first()
        egress_banned: list[str] = []
        if direct is not None:
            cond = (SourceProxyHealth.proxy_id == direct.id) & (
                (SourceProxyHealth.state == "open")
                | (SourceProxyHealth.permanent.is_(True)))
            rows = session.query(SourceProxyHealth.source).filter(cond).all()
            egress_banned = sorted({r[0] for r in rows})
        out.append({
            "id": c.id, "name": c.name, "enabled": c.enabled,
            "namespace": c.namespace, "capacity": c.capacity,
            "open_runs": open_runs, "reachable": reachable,
            "tags": c.tags or [],
            "api_server": c.api_server,
            "job_states": job_states,
            "egress_known": direct is not None,
            "egress_banned": egress_banned[:5],
            "egress_banned_count": len(egress_banned),
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

    Every count is filtered to the requested window (fix-silent-zero-yield-
    crawls R6: a windowed query must never return the lifetime table).
    """
    if hours <= 0:
        hours = 24
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


# --- next-fire projection (add-panel-crawl-observability) ---------------------
def next_runs(session: Session, now: dt.datetime | None = None) -> list[dict]:
    """Per enabled policy, the next cron fire in the policy's own timezone.

    Forward-looking projection for the panel home + the ``crawl_status``
    schedule section. Base is ``last_run_at`` (or ``created_at`` when never
    run) — the same reference ``_cron_due`` uses, so what the panel shows and
    when the reconciler fires agree. Single-flight is deliberately NOT folded
    in: a policy whose fire would be skipped due to an open run is shown at
    its raw next fire; the running-runs section shows the open run next to
    it, which is the truthful picture. The digest keeps ``today_scheduled``
    (its "what fired today" semantics are digest-shaped).
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    out: list[dict] = []
    for p in session.query(CrawlPolicy).filter_by(enabled=True).all():
        try:
            ptz = _policy_tz(p)
            base = _as_aware_utc(p.last_run_at) or _as_aware_utc(p.created_at)
            if base is None:
                fire_local = croniter(p.cron_expr, now.astimezone(ptz)).get_next(dt.datetime)
            else:
                fire_local = croniter(p.cron_expr, base.astimezone(ptz)).get_next(dt.datetime)
                # "next" is future-looking for the panel: if the base-derived
                # fire already passed (an overdue or already-executed schedule),
                # project from now instead of showing a stale timestamp.
                base_fire_utc = fire_local.astimezone(dt.timezone.utc)
                if base_fire_utc <= now:
                    fire_local = croniter(p.cron_expr, now.astimezone(ptz)).get_next(dt.datetime)
        except Exception as e:  # noqa: BLE001 - a bad cron must not break the projection
            logger.warning("next_runs: policy %s cron parse failed: %s", p.name, e)
            continue
        fire_utc = fire_local.astimezone(dt.timezone.utc)
        out.append({
            "policy_id": p.id, "policy": p.name,
            "frequency": p.frequency, "cron_expr": p.cron_expr,
            "timezone": p.timezone or "UTC",
            # UTC instant is the sort key; the local rendering is what a human reads
            "next_fire": fire_utc.isoformat(),
            "next_fire_local": fire_local.isoformat(),
            "minutes_until": int((fire_utc - now).total_seconds() // 60),
        })
    out.sort(key=lambda x: x["next_fire"])
    return out


def missed_runs(
    session: Session, now: dt.datetime | None = None, grace_min: int | None = None,
) -> list[dict]:
    """Enabled policies whose oldest uncovered fire passed more than a grace
    interval ago without a run recording a (non-failed) outcome
    (panel-ops-console).

    Uses the same cron/timezone base (``last_run_at``/``created_at``, policy
    tz) as ``next_runs``/``_cron_due``, so the red flag and the schedule agree.
    The flagged fire is the FIRST fire after the base — the oldest fire a run
    could have covered; using the latest fire instead would let every new fire
    reset the clock and a nightly policy would never flag. Default grace =
    max(2× schedule interval, 30 min) from that fire's cadence. Reasons are
    heuristic labels: an open run started before the fire → "blocked by
    single-flight"; a failed run with a "refused:" detail → "plan refused";
    any other failed run → "launch failed"; otherwise "never launched".
    Display-only — no automatic re-launch.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    out: list[dict] = []
    for p in session.query(CrawlPolicy).filter_by(enabled=True).all():
        try:
            ptz = _policy_tz(p)
            base = _as_aware_utc(p.last_run_at) or _as_aware_utc(p.created_at)
            if base is None:
                continue  # never-run and never-scheduled: nothing owed yet
            fire_local = croniter(p.cron_expr, base.astimezone(ptz)).get_next(dt.datetime)
            fire_utc = fire_local.astimezone(dt.timezone.utc)
            if fire_utc > now:
                continue  # the next fire is still in the future: nothing owed
            # per-policy grace from this cron's cadence: gap to the next fire
            nxt = croniter(p.cron_expr, fire_local).get_next(dt.datetime)
            interval_min = (nxt - fire_local).total_seconds() / 60
            grace = grace_min if grace_min is not None else max(2 * interval_min, 30)
            if (now - fire_utc).total_seconds() / 60 <= grace:
                continue  # inside the grace window
            fire_naive = fire_utc.replace(tzinfo=None)  # naive-UTC column contract
            runs_after = (session.query(PolicyRun)
                          .filter(PolicyRun.policy_id == p.id,
                                  PolicyRun.started_at >= fire_naive)
                          .order_by(PolicyRun.started_at.desc())
                          .all())
            # the flag clears once a run records a (non-failed) outcome; a
            # refused/failed attempt keeps the flag up with its reason
            if any(r.status != "failed" for r in runs_after):
                continue
            reason = "never launched"
            if runs_after:
                refused = any((r.detail or "").startswith("refused:")
                              for r in runs_after)
                reason = "plan refused" if refused else "launch failed"
            elif (session.query(PolicyRun)
                  .filter(PolicyRun.policy_id == p.id,
                          PolicyRun.started_at < fire_naive,
                          PolicyRun.status == "running")
                  .first()) is not None:
                reason = "blocked by single-flight"
            out.append({
                "policy_id": p.id, "policy": p.name,
                "missed_fire": fire_utc.isoformat(),
                "missed_fire_local": fire_local.isoformat(),
                "timezone": p.timezone or "UTC",
                "grace_min": int(grace),
                "minutes_late": int((now - fire_utc).total_seconds() // 60),
                "reason": reason,
            })
        except Exception as e:  # noqa: BLE001 - a bad cron must not break the board
            logger.warning("missed_runs: policy %s skipped: %s", p.name, e)
            continue
    out.sort(key=lambda x: x["missed_fire"])
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


# --- redundant-policy streak + fleet yield (fix-silent-zero-yield-crawls) ----
_REDUNDANT_STREAK_N = int(__import__("os").environ.get("SCRAW_REDUNDANT_STREAK_N", "3"))


def redundant_streaks(session: Session, n: int = _REDUNDANT_STREAK_N) -> list[dict]:
    """Enabled policies whose last N runs ALL closed ``redundant``.

    A permanently frozen date window produces real network traffic and real
    success-looking runs forever; the streak is what makes it visible (spec
    crawl-visibility: redundant-policy streak surfaced in the digest).
    """
    out: list[dict] = []
    policies = session.query(CrawlPolicy).filter_by(enabled=True).all()
    for p in policies:
        runs = (session.query(PolicyRun.status)
                .filter_by(policy_id=p.id)
                .order_by(PolicyRun.started_at.desc())
                .limit(n).all())
        if len(runs) < n or any(status != "redundant" for (status,) in runs):
            continue
        out.append({
            "policy_id": p.id, "policy": p.name,
            "streak": len(runs),
            "date_policy": p.date_policy,
        })
    return out


def fleet_yield(session: Session, hours: int = 24) -> dict:
    """Total rows_new / rows_attempted across runs in the window — the single
    number that makes a zero-acquisition DAY visible (spec: digest reports
    fleet yield)."""
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    since_naive = since.replace(tzinfo=None)
    row = (
        session.query(
            func.coalesce(func.sum(PolicyRun.rows_attempted), 0),
            func.coalesce(func.sum(PolicyRun.rows_new), 0),
            func.count(PolicyRun.id),
        )
        .filter(PolicyRun.started_at > since_naive,
                PolicyRun.status != "running")
        .one()
    )
    return {"rows_attempted": int(row[0]), "rows_new": int(row[1]),
            "runs": int(row[2]), "window_hours": hours}


# --- running runs (add-panel-crawl-observability) -----------------------------
def running_runs(session: Session, now: dt.datetime | None = None) -> list[dict]:
    """Every open ``policy_runs`` row with live yield counters + cluster name.

    ``rows_attempted``/``rows_new`` are updated incrementally by the crawling
    pod (keyed by ``SCRAW_JOB_REF``), so reading them here is live progress —
    no new reporting mechanism. Included in ``build_snapshot`` for the panel
    home; ``recent_runs`` is not guaranteed to contain long-running rows.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    rows = (
        session.query(PolicyRun, CrawlPolicy.name, Cluster.name)
        .join(CrawlPolicy, PolicyRun.policy_id == CrawlPolicy.id)
        .outerjoin(Cluster, PolicyRun.cluster_id == Cluster.id)
        .filter(PolicyRun.status == "running")
        .order_by(PolicyRun.started_at.asc())
        .all()
    )
    out = []
    for run, pname, cname in rows:
        started = _as_aware_utc(run.started_at) or now
        elapsed_min = int((now - started).total_seconds() // 60)
        out.append({
            "id": run.id, "policy_id": run.policy_id, "policy": pname,
            "status": run.status, "cluster": cname, "cluster_id": run.cluster_id,
            "job_ref": run.job_ref,
            "started_at": _iso(run.started_at),
            "elapsed_minutes": elapsed_min,
            "plan_cells": run.plan_cells,
            "rows_attempted": run.rows_attempted,
            "rows_new": run.rows_new,
        })
    return out


# --- crawl platform: sources / runs / health (crawl-platform 4.1/4.3) ---------
# Shared by the panel platform views AND the platform_* MCP tools so the two
# control-plane entrances always agree (spec crawl-control-plane: same facts,
# same guardrails). Read-only over crawl_sources / crawl_runs / pending_runs.

# A lit source (schedule non-null) with no crawl_runs record for this many days
# shows a stall hint — a prompt, not an error (spec: 停滞提示而非按无数据处理).
PLATFORM_STALLED_DAYS = 7


def platform_sources(session: Session, site: str | None = None,
                     now: dt.datetime | None = None) -> list[dict]:
    """Every ``crawl_sources`` row joined with its latest ``crawl_runs`` fact.

    Per source: site, schedule (None = 未点亮 not lit), enabled, kind
    ('platform' | 'federated') with frozen_reason, last run
    (status/time/rows_written/id), the count of ACTIVE pending_runs (pending |
    claimed), and a ``stalled`` hint — True only for sources that are expected
    to run (enabled AND schedule lit) whose last run is absent or older than
    PLATFORM_STALLED_DAYS. Unlit / disabled sources are shown as such, never
    stalled: nothing is owed.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff_naive = (now - dt.timedelta(days=PLATFORM_STALLED_DAYS)).replace(
        tzinfo=None)  # naive-UTC comparison contract, see stale_runs

    latest = (
        session.query(
            CrawlRun.source.label("src"),
            CrawlRun.id.label("rid"),
            CrawlRun.status.label("rstatus"),
            CrawlRun.started_at.label("rstart"),
            CrawlRun.rows_written.label("rrows"),
            func.row_number().over(
                partition_by=CrawlRun.source,
                order_by=(CrawlRun.started_at.desc().nulls_last(),
                          CrawlRun.id.desc())).label("rn"),
        ).subquery()
    )
    q = (
        session.query(
            CrawlSource, latest.c.rid, latest.c.rstatus, latest.c.rstart,
            latest.c.rrows)
        .outerjoin(latest, and_(latest.c.src == CrawlSource.source,
                                latest.c.rn == 1))
        .order_by(CrawlSource.source)
    )
    if site is not None:
        q = q.filter(CrawlSource.site == site)

    pending_counts: dict[str, int] = dict(
        session.query(PendingRun.source, func.count(PendingRun.id))
        .filter(PendingRun.status.in_(("pending", "claimed")))
        .group_by(PendingRun.source).all())

    out = []
    for src, rid, rstatus, rstart, rrows in q.all():
        last_start = _as_aware_utc(rstart)
        stalled = bool(
            src.enabled and src.schedule
            and (last_start is None
                 or (now - last_start).total_seconds() > PLATFORM_STALLED_DAYS * 86400))
        out.append({
            "source": src.source, "site": src.site,
            "schedule": src.schedule, "enabled": src.enabled,
            "last_commit": src.last_commit,
            "updated_at": _iso(src.updated_at),
            "kind": src.kind, "frozen_reason": src.frozen_reason,
            "runner_image": src.runner_image,
            "last_run_id": rid,
            "last_run_status": rstatus,
            "last_run_at": _iso(rstart),
            "last_run_rows": int(rrows) if rrows is not None else None,
            "pending": int(pending_counts.get(src.source, 0)),
            "stalled": stalled,
        })
    return out


def platform_runs(session: Session, source: str | None = None,
                  limit: int = 20) -> list[dict]:
    """Latest ``crawl_runs`` rows with their pending_runs trigger lineage
    (requested_by) when the run was triggered from the control plane."""
    q = (
        session.query(CrawlRun, PendingRun.requested_by, PendingRun.status)
        .outerjoin(PendingRun, CrawlRun.pending_run_id == PendingRun.id)
        .order_by(CrawlRun.started_at.desc().nulls_last(), CrawlRun.id.desc())
    )
    if source is not None:
        q = q.filter(CrawlRun.source == source)
    q = q.limit(limit)
    out = []
    for run, requested_by, pending_status in q.all():
        d = run.toDict()
        d["pending_requested_by"] = requested_by
        d["pending_status"] = pending_status
        out.append(d)
    return out


def platform_health(session: Session, hours: int = 24,
                    now: dt.datetime | None = None) -> dict:
    """Cockpit roll-up: total registered sources, lit (schedule non-null),
    stalled (see ``platform_sources``), and platform-run success/failed counts
    over the window (finished_at-based, like kpi_snapshot)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    since_naive = (now - dt.timedelta(hours=hours)).replace(tzinfo=None)
    by_status: dict[str, int] = dict(
        session.query(CrawlRun.status, func.count(CrawlRun.id))
        .filter(CrawlRun.finished_at >= since_naive)
        .group_by(CrawlRun.status).all())
    sources = session.query(
        func.count(CrawlSource.source),
        func.count(CrawlSource.schedule),
    ).one()
    total, lit = int(sources[0] or 0), int(sources[1] or 0)
    stalled = sum(1 for s in platform_sources(session, now=now) if s["stalled"])
    return {
        "total": total, "lit": lit, "stalled": stalled,
        "success_24h": by_status.get("success", 0),
        "failed_24h": by_status.get("failed", 0),
        "window_hours": hours,
    }


# --- source-discovery pipeline funnel (harness-platform-integration) --------
def discovery_funnel(session: Session, pending_limit: int = 10) -> dict:
    """Read-only funnel over the central pipeline tables (discoveries /
    candidates / analyses / manifests) plus the landed linkage.

    Stage counts — discoveries / candidates / analyses (row counts),
    generated (all manifests), pending (status='draft'), approved
    (status='approved'), landed: approved manifests whose source_name
    exactly matches a crawl_sources.source. Landed is a QUERY-TIME FACT —
    no FK, no schema change (spec source-discovery-pipeline).

    Also returns the latest draft manifests (the approval queue — approval
    itself happens on the harness tool surface, this is read-only) and the
    latest approved manifests each with its landed flag. Shared by the
    panel funnel view and (future) MCP tools, like every query here.
    """
    landed_names = {r[0] for r in session.query(CrawlSource.source).all()}

    def _count(q) -> int:
        return int(q.scalar() or 0)

    approved_rows = (
        session.query(SourceManifest)
        .filter(SourceManifest.status == "approved")
        .order_by(SourceManifest.updated_at.desc().nulls_last(),
                  SourceManifest.id.desc())
        .all())
    counts = {
        "discoveries": _count(session.query(func.count(Discovery.id))),
        "candidates": _count(session.query(func.count(Candidate.id))),
        "analyses": _count(session.query(func.count(Analysis.id))),
        "generated": _count(session.query(func.count(SourceManifest.id))),
        "pending": _count(session.query(func.count(SourceManifest.id))
                          .filter(SourceManifest.status == "draft")),
        "approved": len(approved_rows),
        "landed": sum(1 for m in approved_rows
                      if (m.source_name or "") in landed_names),
    }

    def _brief(m: SourceManifest, *, landed: bool = False) -> dict:
        d = {"id": m.id, "source_name": m.source_name,
             "model_used": m.model_used,
             "updated_at": _iso(m.updated_at) or _iso(m.created_at)}
        if landed:
            # only approved manifests can be landed (spec: draft never lands)
            d["landed"] = bool(m.source_name) and m.source_name in landed_names
        return d

    queue = (
        session.query(SourceManifest)
        .filter(SourceManifest.status == "draft")
        .order_by(SourceManifest.updated_at.desc().nulls_last(),
                  SourceManifest.id.desc())
        .limit(pending_limit).all())
    return {
        "counts": counts,
        "pending": [_brief(m) for m in queue],
        "approved": [_brief(m, landed=True)
                     for m in approved_rows[:pending_limit]],
    }


def source_pipeline_link(session: Session, source: str) -> dict | None:
    """Discovery-pipeline provenance for one landed source: the latest
    approved manifest whose source_name equals this crawl_sources.source.
    Query-time fact like ``discovery_funnel``'s landed count — never writes."""
    m = (session.query(SourceManifest)
         .filter(SourceManifest.source_name == source,
                 SourceManifest.status == "approved")
         .order_by(SourceManifest.updated_at.desc().nulls_last(),
                   SourceManifest.id.desc())
         .first())
    if m is None:
        return None
    return {"manifest_id": m.id, "source_name": m.source_name,
            "model_used": m.model_used,
            "updated_at": _iso(m.updated_at) or _iso(m.created_at)}


# --- authenticated-crawling identity pool (session-pool 4.1/4.2) --------------
# Read-only aggregations over crawl_identities / crawl_identity_events shared
# by the Console auth panel and the auth_* MCP tools — same contract as the
# platform_* functions above: one query set, two entrances, identical facts.
# The panel never writes these tables; logins happen on the login site.

IDENTITY_EVENT_STREAM_LIMIT = 20  # the panel event stream shows the latest 20


def identity_pool(session: Session, source: str | None = None,
                  now: dt.datetime | None = None) -> list[dict]:
    """Every ``crawl_identities`` row (optionally one source) as a display
    row: status, automation, last login/success, the zero-run streak, and the
    lease view — ``leased`` only while a lease is held AND its TTL has not
    passed; a held-but-past-TTL lease reads ``lease_expired`` (the lazy
    re-claimable state the table-semantics lease defines)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    q = session.query(CrawlIdentity).order_by(
        CrawlIdentity.source, CrawlIdentity.account_alias)
    if source is not None:
        q = q.filter(CrawlIdentity.source == source)
    out = []
    for i in q.all():
        expires = _as_aware_utc(i.lease_expires_at)
        holds_lease = i.lease_owner is not None
        out.append({
            "id": i.id, "source": i.source,
            "account_alias": i.account_alias, "status": i.status,
            "automation": i.automation, "egress_ref": i.egress_ref,
            "last_login_at": _iso(i.last_login_at),
            "last_probe_at": _iso(i.last_probe_at),
            "last_success_at": _iso(i.last_success_at),
            "consecutive_zero_runs": i.consecutive_zero_runs,
            "failure_count": i.failure_count,
            "lease_owner": i.lease_owner,
            "leased": holds_lease and (expires is None or expires > now),
            "lease_expired": holds_lease and expires is not None and expires <= now,
        })
    return out


def identity_pool_summary(session: Session, source: str | None = None,
                          now: dt.datetime | None = None) -> dict:
    """The ``auth_status`` payload: identity totals, the login-required queue
    length, and per source the status distribution (five states) + the
    currently-leased count. With ``source``: also that source's detail rows
    (``identities``, empty when it has none — an unknown source is not an
    error, it just owns no pool)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    rows = identity_pool(session, source=source, now=now)
    by_source: dict[str, dict] = {}
    for r in rows:
        bucket = by_source.setdefault(r["source"], {
            "source": r["source"], "total": 0, "leased": 0, "statuses": {}})
        bucket["total"] += 1
        bucket["statuses"][r["status"]] = bucket["statuses"].get(r["status"], 0) + 1
        if r["leased"]:
            bucket["leased"] += 1
    out = {
        "total_identities": len(rows),
        "login_required": sum(b["statuses"].get("login_required", 0)
                              for b in by_source.values()),
        "active": sum(b["statuses"].get("active", 0)
                      for b in by_source.values()),
        "leased": sum(b["leased"] for b in by_source.values()),
        "sources": [by_source[k] for k in sorted(by_source)],
    }
    if source is not None:
        out["source"] = source
        out["identities"] = rows
    return out


def login_queue(session: Session) -> list[dict]:
    """The 需登录队列: identities with status='login_required', most-failed
    first, then longest-since-last-login (never-logged-in sorts first —
    registrations await their first login)."""
    rows = [r for r in identity_pool(session) if r["status"] == "login_required"]
    rows.sort(key=lambda r: (-r["failure_count"], r["last_login_at"] or ""))
    return rows


def identity_events(session: Session, source: str | None = None,
                    limit: int = IDENTITY_EVENT_STREAM_LIMIT) -> list[dict]:
    """Newest ``crawl_identity_events`` with their identity (source + alias)
    attached, newest first, capped at ``limit`` (default: the panel stream's
    latest 20; hard cap 200)."""
    limit = max(1, min(int(limit or IDENTITY_EVENT_STREAM_LIMIT), 200))
    q = (
        session.query(CrawlIdentityEvent,
                      CrawlIdentity.source, CrawlIdentity.account_alias)
        .join(CrawlIdentity, CrawlIdentityEvent.identity_id == CrawlIdentity.id)
        .order_by(CrawlIdentityEvent.created_at.desc().nulls_last(),
                  CrawlIdentityEvent.id.desc())
    )
    if source is not None:
        q = q.filter(CrawlIdentity.source == source)
    out = []
    for ev, src, alias in q.limit(limit).all():
        out.append({
            "id": ev.id, "source": src, "account_alias": alias,
            "kind": ev.kind, "detail": ev.detail,
            "lease_token": ev.lease_token, "created_at": _iso(ev.created_at),
        })
    return out


# --- the composite snapshot --------------------------------------------------
def build_snapshot(session: Session, *, hours: int = 24, run_limit: int = 20) -> dict:
    """Full datasource-centric snapshot — the one call the digest + tool share."""
    runs = recent_runs(session, limit=run_limit)
    fleet = fleet_health(session)
    stale = stale_runs(session)
    sources = per_source_outcome(session, hours=hours)
    circuits = circuit_state(session)
    scheduled = today_scheduled(session)
    streaks = redundant_streaks(session)
    yield_summary = fleet_yield(session, hours=hours)
    running = running_runs(session)
    upcoming = next_runs(session)
    # roll-up counts. Labels carry the ACTUAL window (fix-silent-zero-yield-
    # crawls R6: a 168h count under a "24h" label is a lie even when the
    # underlying filter is right).
    n_ok = sum(r["ok"] for r in sources)
    n_err = sum(r["err"] for r in sources)
    return {
        "generated_at": _iso(dt.datetime.now(dt.timezone.utc)),
        "window_hours": hours,
        "recent_runs": runs,
        "running_runs": running,
        "fleet": fleet,
        "stale_runs": stale,
        "per_source_outcome": sources,
        "circuit_state": circuits,
        "today_scheduled": scheduled,
        "next_runs": upcoming,
        "redundant_streaks": streaks,
        "fleet_yield": yield_summary,
        "summary": {
            f"fetches_ok_{hours}h": n_ok,
            f"fetches_err_{hours}h": n_err,
            "stale_run_count": len(stale),
            "fleet_enabled": sum(1 for f in fleet if f["enabled"]),
            "fleet_unreachable": [f["name"] for f in fleet if f["enabled"] and f["reachable"] is False],
            "rows_new_window": yield_summary["rows_new"],
        },
    }
