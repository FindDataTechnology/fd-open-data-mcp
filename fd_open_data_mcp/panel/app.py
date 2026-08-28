"""Panel routes: observability home + partials, run detail, data coverage,
policy list + toggles, editor with estimate preview, runs view.

Served standalone (``uvicorn fd_open_data_mcp.panel.app:app`` / CLI ``panel``)
or mounted under /panel via ``mcp.http_app().mount``. All routes hit the same
``crawl_policies``/``policy_runs`` tables as the MCP tools.
"""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func

from fd_open_data_mcp.db import get_database
from fd_open_data_mcp.models import (
    Cluster, Concept, CrawlPolicy, FetchLog, PolicyRun,
)
from fd_open_data_mcp.visibility import snapshot as _snapshot

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))

FREQUENCIES = ["daily", "weekly", "monthly", "quarterly", "yearly"]
MODES = ["series", "per_date"]
DATE_POLICY_MODES = ["since_last", "trailing", "explicit"]

# htmx poll cadence for the home partials (design D2) and the reconciler
# liveness banner threshold (design: a suspended scheduler must be visible).
POLL_SECONDS = 15
RECONCILER_QUIET_HOURS = 24


def _session():
    return get_database().get_session()


def _policy_or_404(s, pid: int) -> CrawlPolicy:
    p = s.query(CrawlPolicy).get(pid)
    if not p:
        raise HTTPException(404, f"policy {pid} not found")
    return p


def _entity_types(s) -> list[str]:
    rows = s.query(CrawlPolicy.entity_type).distinct().all()
    return [r[0] for r in rows]


def _run_estimate(s, plan_json: dict | None) -> int:
    from fd_open_data_mcp.crawl.plan import CrawlPlan
    from fd_open_data_mcp.refresh.reconciler import estimate_fetches

    if not plan_json:
        return 0
    try:
        return estimate_fetches(s, CrawlPlan.model_validate(plan_json))
    except Exception:  # noqa: BLE001
        return 0


def _concept_groups(s) -> list[tuple[str, list[Concept]]]:
    """Concepts grouped by category for the editor multi-select."""
    groups: dict[str, list[Concept]] = {}
    for c in s.query(Concept).filter_by(deprecated=False).order_by(Concept.code).all():
        cat = c.category or (c.code.split(".")[0] if "." in c.code else c.code)
        groups.setdefault(cat, []).append(c)
    return sorted(groups.items())


def _parse_concept_ids(form_concepts: list[str]) -> list[int]:
    return [int(x) for x in form_concepts if x]


def _parse_entity_ids(raw: str) -> list[int] | None:
    ids = [int(x) for x in raw.replace(" ", "").split(",") if x]
    return ids or None  # empty -> all entities


def _parse_source_filter(raw: str) -> list[str] | None:
    vals = [x.strip() for x in raw.split(",") if x.strip()]
    return vals or None


def _parse_date_policy(
    mode: str, days: str, start: str, end: str,
) -> dict:
    if mode == "trailing":
        return {"mode": "trailing", "days": int(days) if days else 1}
    if mode == "explicit":
        return {"mode": "explicit", "start": start or None, "end": end or None}
    return {"mode": "since_last"}


def _policy_from_form(form) -> dict:
    """Extract a policy payload dict from a submitted form."""
    return {
        "name": form.get("name", "").strip(),
        "enabled": "enabled" in form,
        "concept_ids": _parse_concept_ids(form.getlist("concept_ids")),
        "entity_type": form["entity_type"],
        "entity_ids": _parse_entity_ids(form.get("entity_ids", "")),
        "date_policy": _parse_date_policy(
            form.get("date_policy_mode", "since_last"),
            form.get("date_policy_days", ""), form.get("date_policy_start", ""),
            form.get("date_policy_end", "")),
        "frequency": form.get("frequency", "daily"),
        "mode": form.get("mode", "per_date"),
        "source_filter": _parse_source_filter(form.get("source_filter", "")),
        "force": "force" in form,
        "cron_expr": form.get("cron_expr", "0 6 * * *"),
        "timezone": form.get("timezone", "UTC"),
    }


def create_app() -> FastAPI:
    app = FastAPI(title="Crawl Control Center")
    app.mount("/panel/static", StaticFiles(directory=str(HERE / "static")),
              name="panel_static")

    # PANEL_TOKEN gate (design D7): header, ?token=, or cookie
    token = os.environ.get("PANEL_TOKEN")

    @app.middleware("http")
    async def gate(request: Request, call_next):
        if not token:
            return await call_next(request)
        q = request.query_params.get("token")
        if q == token:
            resp = await call_next(request)
            resp.set_cookie("panel_token", token)
            return resp
        if request.headers.get("X-Panel-Token") == token or request.cookies.get("panel_token") == token:
            return await call_next(request)
        return HTMLResponse("<h1>401 - set PANEL_TOKEN / ?token=</h1>", status_code=401)

    # ── pages ──────────────────────────────────────────────────────────────
    @app.get("/", response_class=HTMLResponse)
    def index():
        return RedirectResponse("/panel")

    def _unavailable(e: Exception) -> HTMLResponse:
        # One failing partial must not fail the page (spec: live panel refresh)
        return HTMLResponse(f'<p class="muted">section unavailable: {e}</p>')

    def _scheduler_quiet(s) -> tuple[bool, str | None]:
        """True when no run has started within RECONCILER_QUIET_HOURS — the
        suspended-reconciler case stays visible instead of silent."""
        last = s.query(func.max(PolicyRun.started_at)).scalar()
        if last is None:
            return True, None
        # TIMESTAMP WITHOUT TIME ZONE column, naive-UTC per the writer contract
        age_h = (dt.datetime.utcnow() - last).total_seconds() / 3600
        return age_h > RECONCILER_QUIET_HOURS, last.isoformat()

    @app.get("/panel", response_class=HTMLResponse)
    def home(request: Request):
        s = _session()
        try:
            quiet, last_started = _scheduler_quiet(s)
            return templates.TemplateResponse(
                request, "home.html",
                {"poll_seconds": POLL_SECONDS,
                 "quiet_hours": RECONCILER_QUIET_HOURS,
                 "scheduler_quiet": quiet, "last_run_started": last_started})
        finally:
            s.close()

    # ── home partials (htmx polling; each degrades independently) ──────────
    @app.get("/panel/partials/running", response_class=HTMLResponse)
    def partial_running(request: Request):
        try:
            s = _session()
            try:
                rows = _snapshot.running_runs(s)
            finally:
                s.close()
            return templates.TemplateResponse(
                request, "partial_running.html", {"runs": rows})
        except Exception as e:  # noqa: BLE001
            return _unavailable(e)

    @app.get("/panel/partials/recent", response_class=HTMLResponse)
    def partial_recent(request: Request):
        try:
            s = _session()
            try:
                rows = _snapshot.recent_runs(s, limit=15)
            finally:
                s.close()
            return templates.TemplateResponse(
                request, "partial_recent.html", {"runs": rows})
        except Exception as e:  # noqa: BLE001
            return _unavailable(e)

    @app.get("/panel/partials/fleet", response_class=HTMLResponse)
    def partial_fleet(request: Request):
        try:
            s = _session()
            try:
                rows = _snapshot.fleet_health(s)
            finally:
                s.close()
            return templates.TemplateResponse(
                request, "partial_fleet.html", {"fleet": rows})
        except Exception as e:  # noqa: BLE001
            return _unavailable(e)

    @app.get("/panel/partials/next", response_class=HTMLResponse)
    def partial_next(request: Request):
        try:
            s = _session()
            try:
                rows = _snapshot.next_runs(s)
            finally:
                s.close()
            return templates.TemplateResponse(
                request, "partial_next.html", {"upcoming": rows})
        except Exception as e:  # noqa: BLE001
            return _unavailable(e)

    # ── run detail ─────────────────────────────────────────────────────────
    @app.get("/panel/runs/{run_id}", response_class=HTMLResponse)
    def run_detail(request: Request, run_id: int):
        s = _session()
        try:
            row = (
                s.query(PolicyRun, CrawlPolicy.name, Cluster.name)
                .join(CrawlPolicy, PolicyRun.policy_id == CrawlPolicy.id)
                .outerjoin(Cluster, PolicyRun.cluster_id == Cluster.id)
                .filter(PolicyRun.id == run_id)
                .first()
            )
            if not row:
                raise HTTPException(404, f"run {run_id} not found")
            run, policy_name, cluster_name = row

            plan = run.plan_json if isinstance(run.plan_json, dict) else {}
            concepts = [
                {"id": pc.get("concept_id"), "code": pc.get("code"),
                 "sources": [rs.get("source") for rs in pc.get("ranked_sources") or []]}
                for pc in plan.get("wanted_concepts") or []
            ]
            scope = plan.get("entity_scope") or {}
            date_range = plan.get("date_range") or {}

            # fetch_log carries no run key (design D5): approximate by the run's
            # window filtered to its plan concepts + cluster, labeled as such.
            fetch_summary: list[dict] = []
            window_end = run.finished_at or dt.datetime.utcnow()
            if run.started_at:
                concept_ids = [c["id"] for c in concepts if c["id"] is not None]
                fq = (
                    s.query(FetchLog.status, func.count(FetchLog.id))
                    .filter(FetchLog.timestamp >= run.started_at,
                            FetchLog.timestamp <= window_end,
                            FetchLog.cluster_id == run.cluster_id)
                )
                if concept_ids:
                    fq = fq.filter(FetchLog.concept_id.in_(concept_ids))
                fetch_summary = [
                    {"status": status, "count": int(cnt)}
                    for status, cnt in fq.group_by(FetchLog.status).all()
                ]
            return templates.TemplateResponse(
                request, "run_detail.html",
                {"run": run.toDict(), "policy_name": policy_name,
                 "cluster_name": cluster_name,
                 "plan": {"mode": plan.get("mode"),
                          "concepts": concepts,
                          "entity_type": scope.get("entity_type"),
                          "entity_ids": scope.get("entity_ids"),
                          "start": date_range.get("start"),
                          "end": date_range.get("end")},
                 "fetch_summary": fetch_summary,
                 "fetch_window": [run.started_at.isoformat() if run.started_at else None,
                                  run.finished_at.isoformat() if run.finished_at else "now"],
                 "duration_min": (int(((run.finished_at or dt.datetime.utcnow())
                                       - run.started_at).total_seconds() // 60)
                                 if run.started_at else None)})
        finally:
            s.close()

    # ── data coverage ──────────────────────────────────────────────────────
    @app.get("/panel/data", response_class=HTMLResponse)
    def data_coverage(request: Request, concept_id: int | None = None,
                      entity_type: str = ""):
        from fd_open_data_mcp.visibility.coverage import coverage_by_concept

        s = _session()
        try:
            rows = coverage_by_concept(s, concept_id=concept_id,
                                       entity_type=entity_type or None)
            total_rows = sum(r["rows"] for r in rows)
            return templates.TemplateResponse(
                request, "data.html",
                {"coverage": rows, "total_rows": total_rows,
                 "n_concepts": len(rows),
                 "concept_id": concept_id, "entity_type": entity_type})
        finally:
            s.close()

    @app.get("/panel/policies", response_class=HTMLResponse)
    def policy_list(request: Request):
        s = _session()
        try:
            policies = s.query(CrawlPolicy).order_by(CrawlPolicy.id).all()
            return templates.TemplateResponse(
                request, "policies.html",
                {"policies": [p.toDict() for p in policies], "entity_types": _entity_types(s)})
        finally:
            s.close()

    @app.get("/panel/runs", response_class=HTMLResponse)
    def runs(request: Request, status: str = "", policy_id: int | None = None):
        s = _session()
        try:
            q = s.query(PolicyRun)
            if status:
                q = q.filter_by(status=status)
            if policy_id is not None:
                q = q.filter_by(policy_id=policy_id)
            runs_rows = q.order_by(PolicyRun.started_at.desc()).limit(200).all()
            policies = {p.id: p.name for p in s.query(CrawlPolicy).all()}
            rendered = []
            for r in runs_rows:
                d = {"id": r.id, "policy": policies.get(r.policy_id, f"#{r.policy_id}"),
                     "policy_id": r.policy_id, "status": r.status, "job_ref": r.job_ref,
                     "started_at": r.started_at.isoformat() if r.started_at else None,
                     "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                     "detail": r.detail}
                d["estimate"] = _run_estimate(s, r.plan_json)
                rendered.append(d)
            return templates.TemplateResponse(
                request, "runs.html",
                {"runs": rendered, "status": status, "policies": policies})
        finally:
            s.close()

    # ── editor ─────────────────────────────────────────────────────────────
    def _editor_context(s, policy: CrawlPolicy | None):
        selected = set(policy.concept_ids) if policy else set()
        return {
            "policy": policy.toDict() if policy else None,
            "concept_groups": _concept_groups(s),
            "selected": selected,
            "entity_types": _entity_types(s),
            "frequencies": FREQUENCIES, "modes": MODES,
            "date_policy_modes": DATE_POLICY_MODES,
            "default_cron": "0 6 * * *",
        }

    @app.get("/panel/policies/new", response_class=HTMLResponse)
    def policy_new(request: Request):
        s = _session()
        try:
            return templates.TemplateResponse(request, "policy_edit.html", _editor_context(s, None))
        finally:
            s.close()

    @app.get("/panel/policies/{policy_id}", response_class=HTMLResponse)
    def policy_edit(request: Request, policy_id: int):
        s = _session()
        try:
            p = _policy_or_404(s, policy_id)
            return templates.TemplateResponse(request, "policy_edit.html", _editor_context(s, p))
        finally:
            s.close()

    @app.post("/panel/policies/save")
    async def policy_save(request: Request):
        form = await request.form()
        payload = _policy_from_form(form)
        if not payload["name"] or not payload["concept_ids"]:
            raise HTTPException(400, "name and at least one concept are required")
        s = _session()
        try:
            pid = form.get("policy_id")
            if pid:
                p = _policy_or_404(s, int(pid))
                for k, v in payload.items():
                    setattr(p, k, v)
            else:
                s.add(CrawlPolicy(**payload))
            s.commit()
        finally:
            s.close()
        return RedirectResponse("/panel/policies", status_code=303)

    @app.post("/panel/policies/{policy_id}/toggle")
    def policy_toggle(policy_id: int):
        s = _session()
        try:
            p = _policy_or_404(s, policy_id)
            p.enabled = not p.enabled
            s.commit()
        finally:
            s.close()
        return RedirectResponse("/panel/policies", status_code=303)

    @app.post("/panel/policies/{policy_id}/delete")
    def policy_delete(policy_id: int):
        s = _session()
        try:
            p = _policy_or_404(s, policy_id)
            s.delete(p)
            s.commit()
        finally:
            s.close()
        return RedirectResponse("/panel/policies", status_code=303)

    @app.post("/panel/policies/{policy_id}/run-now")
    def policy_run_now(policy_id: int):
        from fd_open_data_mcp.refresh.reconciler import _default_launcher, launch_policy

        s = _session()
        try:
            p = _policy_or_404(s, policy_id)
            result = launch_policy(s, p, _default_launcher())
        finally:
            s.close()
        return RedirectResponse(f"/panel/runs?policy_id={policy_id}", status_code=303)

    # ── estimate preview (htmx partial) ────────────────────────────────────
    @app.post("/panel/estimate", response_class=HTMLResponse)
    async def estimate(request: Request):
        payload = _policy_from_form(await request.form())
        s = _session()
        try:
            from fd_open_data_mcp.crawl.plan import EntityScope
            from fd_open_data_mcp.crawl.planner import plan_crawl
            from fd_open_data_mcp.refresh.reconciler import (
                POLICY_MAX_FETCHES, build_date_range, estimate_fetches)

            class _P:  # transient policy-like for build_date_range
                date_policy = payload["date_policy"]
                frequency = payload["frequency"]
            import datetime as _dt
            dr, since_last = build_date_range(_P(), _dt.date.today())
            plan = plan_crawl(
                s, payload["concept_ids"],
                EntityScope(entity_type=payload["entity_type"], entity_ids=payload["entity_ids"]),
                dr, since_last=since_last, source_filter=payload["source_filter"],
                mode=payload["mode"])
            est = estimate_fetches(s, plan)
            return templates.TemplateResponse(
                request, "estimate.html", {
                    "estimate": est, "policy_max": POLICY_MAX_FETCHES,
                    "mode": payload["mode"],
                    "n_concepts": len(plan.wanted_concepts),
                    "unroutable": plan.unroutable, "unmapped": plan.unmapped})
        except Exception as e:  # noqa: BLE001 - the preview must never 500 the editor
            return HTMLResponse(f"<span style='color:#c0392b'>estimate failed: {e}</span>")
        finally:
            s.close()

    return app


app = create_app()
