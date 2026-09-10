"""Panel routes: observability home + partials, run detail, data coverage,
policy list + toggles, editor with estimate preview, runs view, proxy ops.

Served standalone (``uvicorn fd_open_data_mcp.panel.app:app`` / CLI ``panel``)
or mounted under /panel via ``mcp.http_app().mount``. All routes hit the same
``crawl_policies``/``policy_runs`` tables as the MCP tools.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import urllib.request
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func

from fd_open_data_mcp.db import get_database
from fd_open_data_mcp.models import (
    BanRule, Cluster, Concept, CrawlPolicy, FetchLog, PolicyRun, Proxy,
    SourceProxyHealth, SourceRateLimit,
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
# census rows older than this show a staleness marker (add-shard-aware-coverage)
CENSUS_STALE_HOURS = 24

# panel-ops-console 6.x: frequency-template proposals. The cron proposals and
# the daily trailing-1d date policy are input aids only — the saved artifact is
# an ordinary policy row (design D7).
_TEMPLATE_CRON = {
    "daily": "0 6 * * *", "weekly": "0 6 * * 1", "monthly": "0 6 1 * *",
    "quarterly": "0 6 1 1,4,7,10 *", "yearly": "0 6 1 1 *",
}


def _template_concepts(s, frequency: str) -> list[Concept]:
    """Hygiene-filtered concept pre-selection for a frequency template:
    enabled concept cadence = the concept's own frequency; deprecated and
    unverified concepts are excluded (spec crawl-control-center)."""
    return (s.query(Concept)
            .filter_by(frequency=frequency, deprecated=False, verified=True)
            .order_by(Concept.code).all())


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


# ── proxy operations (panel-ops-console) ─────────────────────────────────────
def _mask_auth(auth: str | None) -> str:
    """Mask a proxy credential for ANY panel rendering — the full value must
    never appear in a panel response body (spec crawl-control-center)."""
    if not auth:
        return "—"
    head = auth.split(":", 1)[0][:2]
    return f"{head}•••••"


def _proxy_control_ready() -> bool:
    """Management actions need the proxy-control API; without it the proxy
    pages degrade to read-only (design D1, ships-dark philosophy)."""
    return bool(os.environ.get("PROXY_CONTROL_URL"))


def _proxy_control(method: str, path: str, payload: dict | None = None) -> dict:
    """POST/PUT a management action to the proxy-control API.

    Raises RuntimeError on any transport/API failure; callers turn that into a
    redirect-with-error rather than a 500."""
    base = os.environ.get("PROXY_CONTROL_URL", "").rstrip("/")
    if not base:
        raise RuntimeError("PROXY_CONTROL_URL is not configured")
    body = json.dumps(payload or {}).encode()
    headers = {"Content-Type": "application/json"}
    mgmt_token = os.environ.get("PROXY_CONTROL_TOKEN")
    if mgmt_token:
        headers["X-Management-Token"] = mgmt_token
    req = urllib.request.Request(f"{base}{path}", data=body, method=method,
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except Exception as e:  # noqa: BLE001 - normalized for the panel UX
        raise RuntimeError(f"proxy-control {method} {path} failed: {e}") from e


def _proxy_redirect(msg: str = "", err: str = "") -> RedirectResponse:
    from urllib.parse import quote
    q = f"?msg={quote(msg)}" if msg else (f"?err={quote(err)}" if err else "")
    return RedirectResponse(f"/panel/proxy{q}", status_code=303)


def _run_launcher():
    """The reconciler's default launcher, resolved lazily (run cancel needs
    its delete path; module-level so tests can stub it)."""
    from fd_open_data_mcp.refresh.reconciler import _default_launcher
    return _default_launcher()


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

    # ── auth gate (panel-logto-auth, design D3) ────────────────────────────
    # Precedence: PANEL_TOKEN (programmatic) → OIDC session cookie → redirect
    # to Logto login (when LOGTO_* configured) → legacy 401. The auth routes
    # themselves and static assets are always public.
    token = os.environ.get("PANEL_TOKEN")
    from fd_open_data_mcp.panel import auth as _auth

    @app.middleware("http")
    async def gate(request: Request, call_next):
        cfg = _auth.logto_config()
        path = request.url.path
        if path.startswith("/panel/auth/") or path.startswith("/panel/static"):
            return await call_next(request)
        if token:
            q = request.query_params.get("token")
            if q == token:
                resp = await call_next(request)
                resp.set_cookie("panel_token", token)
                return resp
            if (request.headers.get("X-Panel-Token") == token
                    or request.cookies.get("panel_token") == token):
                return await call_next(request)
        session = _auth.read_session(request.cookies.get(_auth.SESSION_COOKIE))
        if session is not None:
            allowed = _auth.allow_list()
            if allowed is None or session["sub"] in allowed:
                request.state.panel_user = session
                return await call_next(request)
            return HTMLResponse("<h1>403 - user not in PANEL_USER_IDS</h1>",
                                status_code=403)
        if cfg is not None:
            return RedirectResponse("/panel/auth/login", status_code=302)
        if token is not None:
            return HTMLResponse("<h1>401 - set PANEL_TOKEN / ?token=</h1>",
                                status_code=401)
        return await call_next(request)  # no gate configured — open panel

    # ── OIDC routes (panel-logto-auth) ─────────────────────────────────────
    @app.get("/panel/auth/login")
    def auth_login():
        cfg = _auth.logto_config()
        if cfg is None:
            return HTMLResponse("<h1>401 - Logto not configured</h1>", status_code=401)
        state, cookie_value = _auth.make_state()
        resp = RedirectResponse(_auth.authorize_url(cfg, state), status_code=302)
        resp.set_cookie(_auth.STATE_COOKIE, cookie_value, httponly=True,
                        samesite="lax", max_age=600)
        return resp

    @app.get("/panel/auth/callback")
    def auth_callback(request: Request, code: str = "", state: str = "",
                      error: str = "", error_description: str = ""):
        cfg = _auth.logto_config()
        if cfg is None:
            return HTMLResponse("<h1>401 - Logto not configured</h1>", status_code=401)
        if error:
            return HTMLResponse(
                f"<h1>login failed</h1><p>{error}: {error_description}</p>",
                status_code=401)
        if not _auth.check_state(request.cookies.get(_auth.STATE_COOKIE), state):
            return HTMLResponse("<h1>401 - bad state</h1>", status_code=401)
        try:
            claims = _auth.id_token_claims(cfg, _auth.exchange_code(cfg, code))
        except Exception as e:  # noqa: BLE001 - provider/network errors
            return HTMLResponse(f"<h1>login failed</h1><p>{e}</p>", status_code=401)
        allowed = _auth.allow_list()
        if allowed is not None and claims.get("sub") not in allowed:
            return HTMLResponse("<h1>403 - user not in PANEL_USER_IDS</h1>",
                                status_code=403)
        resp = RedirectResponse("/panel", status_code=302)
        resp.set_cookie(_auth.SESSION_COOKIE,
                        _auth.make_session_value(claims.get("sub", ""),
                                                 claims.get("name", "")),
                        httponly=True, samesite="lax",
                        max_age=_auth.SESSION_HOURS * 3600)
        resp.delete_cookie(_auth.STATE_COOKIE)
        return resp

    @app.get("/panel/auth/logout")
    def auth_logout():
        resp = RedirectResponse("/panel", status_code=302)
        resp.delete_cookie(_auth.SESSION_COOKIE)
        return resp

    @app.get("/panel/auth/whoami", response_class=HTMLResponse)
    def auth_whoami(request: Request):
        session = getattr(request.state, "panel_user", None) or _auth.read_session(
            request.cookies.get(_auth.SESSION_COOKIE))
        if session is None:
            return HTMLResponse("")
        return HTMLResponse(
            f'<span class="muted">{session["name"]}</span> '
            f'<a href="/panel/auth/logout" class="btn">logout</a>')

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

    @app.get("/panel/partials/missed", response_class=HTMLResponse)
    def partial_missed(request: Request):
        """Missed-run board (panel-ops-console): enabled policies whose latest
        expected fire passed the grace interval with no successful run."""
        try:
            s = _session()
            try:
                rows = _snapshot.missed_runs(s)
            finally:
                s.close()
            return templates.TemplateResponse(
                request, "partial_missed.html", {"missed": rows})
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
    def _census_rows(s) -> list[dict]:
        """Stored census rows + staleness flags; never collects (design D3)."""
        from fd_open_data_mcp.visibility.census import latest_census

        out = []
        now = dt.datetime.utcnow()
        for r in latest_census(s):
            sampled = r.get("sampled_at")
            age_h = None
            if sampled:
                t = dt.datetime.fromisoformat(sampled)
                if t.tzinfo is not None:
                    t = t.replace(tzinfo=None)
                age_h = (now - t).total_seconds() / 3600
            r["age_hours"] = round(age_h, 1) if age_h is not None else None
            r["stale"] = age_h is None or age_h > CENSUS_STALE_HOURS
            out.append(r)
        return out

    @app.get("/panel/data", response_class=HTMLResponse)
    def data_coverage(request: Request, concept_id: int | None = None,
                      entity_type: str = ""):
        from fd_open_data_mcp.visibility.coverage import coverage_by_concept

        s = _session()
        try:
            rows = coverage_by_concept(s, concept_id=concept_id,
                                       entity_type=entity_type or None)
            total_rows = sum(r["rows"] for r in rows)
            census_rows = _census_rows(s)
            return templates.TemplateResponse(
                request, "data.html",
                {"coverage": rows, "total_rows": total_rows,
                 "n_concepts": len(rows),
                 "census": census_rows,
                 "census_total": sum(r.get("approx_rows") or 0 for r in census_rows),
                 "concept_id": concept_id, "entity_type": entity_type})
        finally:
            s.close()

    @app.post("/panel/data/census/refresh")
    def data_census_refresh():
        from fd_open_data_mcp.visibility.census import refresh_census

        s = _session()
        try:
            refresh_census(s)
        finally:
            s.close()
        return RedirectResponse("/panel/data", status_code=303)

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
                     "policy_id": r.policy_id, "origin": r.origin,
                     "status": r.status, "job_ref": r.job_ref,
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

    # ── run control (panel-ops-console) ─────────────────────────────────────
    @app.post("/panel/runs/{run_id}/cancel")
    def run_cancel(run_id: int):
        """Cancel an open run: CAS row close + best-effort Job deletion."""
        from fd_open_data_mcp.refresh.runs import cancel_run

        s = _session()
        try:
            out = cancel_run(s, run_id, actor="panel",
                             launcher=_run_launcher())
        finally:
            s.close()
        if out["status"] == "cancelled":
            return RedirectResponse("/panel/runs", status_code=303)
        if out["status"] == "not_found":
            raise HTTPException(404, f"run {run_id} not found")
        raise HTTPException(409, f"run {run_id} already finished "
                                 f"({out.get('current_status')}); nothing cancelled")

    @app.post("/panel/clusters/{cluster_id}/capacity")
    async def cluster_capacity(cluster_id: int, request: Request):
        """Edit a cluster's max-concurrent-open-runs; effective next tick."""
        form = await request.form()
        try:
            value = int(form.get("capacity", ""))
        except (TypeError, ValueError):
            raise HTTPException(400, "capacity must be an integer")
        if value < 0:
            raise HTTPException(400, "capacity must be >= 0")
        s = _session()
        try:
            c = s.get(Cluster, cluster_id)
            if c is None:
                raise HTTPException(404, f"cluster {cluster_id} not found")
            c.capacity = value
            s.commit()
        finally:
            s.close()
        return RedirectResponse("/panel", status_code=303)

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
    def policy_new(request: Request, msg: str = "", err: str = ""):
        s = _session()
        try:
            ctx = _editor_context(s, None)
            ctx.update(msg=msg, err=err)
            return templates.TemplateResponse(request, "policy_edit.html", ctx)
        finally:
            s.close()

    @app.get("/panel/policies/template", response_class=HTMLResponse)
    def policy_template(request: Request, frequency: str = "daily"):
        """Frequency-driven policy template (panel-ops-console, design D7):
        pre-selects the hygiene-filtered concepts of that frequency and
        proposes the matching cron + date policy. Input aid only — the saved
        artifact is an ordinary policy row."""
        s = _session()
        try:
            concepts = _template_concepts(s, frequency)
            pseudo = {
                "frequency": frequency, "mode": "per_date", "enabled": True,
                "cron_expr": _TEMPLATE_CRON.get(frequency, "0 6 * * *"),
                "date_policy": ({"mode": "trailing", "days": 1}
                                if frequency == "daily" else {"mode": "since_last"}),
            }
            ctx = _editor_context(s, None)
            ctx.update(policy=pseudo, is_template=True,
                       template_frequency=frequency,
                       selected={c.id for c in concepts})
            return templates.TemplateResponse(request, "policy_edit.html", ctx)
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
    def _compile_plan(s, payload: dict):
        """Compile a payload (as produced by _policy_from_form) into a plan —
        shared by the estimate preview and the ad-hoc launch."""
        from fd_open_data_mcp.crawl.plan import EntityScope
        from fd_open_data_mcp.crawl.planner import plan_crawl
        from fd_open_data_mcp.refresh.reconciler import build_date_range

        class _P:  # transient policy-like for build_date_range
            date_policy = payload["date_policy"]
            frequency = payload["frequency"]
        dr, since_last = build_date_range(_P(), dt.date.today())
        return plan_crawl(
            s, payload["concept_ids"],
            EntityScope(entity_type=payload["entity_type"], entity_ids=payload["entity_ids"]),
            dr, since_last=since_last, source_filter=payload["source_filter"],
            mode=payload["mode"])

    @app.post("/panel/estimate", response_class=HTMLResponse)
    async def estimate(request: Request):
        payload = _policy_from_form(await request.form())
        s = _session()
        try:
            from fd_open_data_mcp.refresh.reconciler import (
                POLICY_MAX_FETCHES, estimate_fetches)

            plan = _compile_plan(s, payload)
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

    # ── ad-hoc one-off crawl (panel-ops-console) ─────────────────────────────
    @app.post("/panel/crawl/adhoc")
    async def crawl_adhoc(request: Request):
        """Run a plan draft once, without creating a policy (design D3)."""
        from urllib.parse import quote

        from fd_open_data_mcp.refresh.runs import launch_adhoc

        payload = _policy_from_form(await request.form())
        s = _session()
        try:
            plan = _compile_plan(s, payload)
            result = launch_adhoc(s, plan, _run_launcher())
        except Exception as e:  # noqa: BLE001 - bad form input: show, don't 500
            return RedirectResponse(f"/panel/policies/new?err={quote(str(e))}",
                                    status_code=303)
        finally:
            s.close()
        if result["status"] == "launched":
            return RedirectResponse("/panel/runs?status=running", status_code=303)
        return RedirectResponse(
            f"/panel/policies/new?err={quote(result['reason'])}", status_code=303)

    # ── proxy operations (panel-ops-console) ─────────────────────────────────
    @app.get("/panel/proxy", response_class=HTMLResponse)
    def proxy_page(request: Request, msg: str = "", err: str = ""):
        s = _session()
        try:
            proxies = s.query(Proxy).order_by(Proxy.provider, Proxy.id).all()
            providers: dict[str, dict] = {}
            for p in proxies:
                bucket = providers.setdefault(p.provider or "(unowned)", {
                    "name": p.provider or "(unowned)", "active": 0, "retired": 0})
                bucket["retired" if p.status == "retired" else "active"] += 1
            limits = s.query(SourceRateLimit).order_by(SourceRateLimit.source).all()
            rules = (s.query(BanRule)
                     .order_by(BanRule.source, BanRule.priority.desc()).limit(200).all())
            return templates.TemplateResponse(request, "proxy.html", {
                "msg": msg, "err": err,
                "mgmt_ready": _proxy_control_ready(),
                "providers": sorted(providers.values(), key=lambda b: b["name"]),
                "proxies": [
                    {"id": p.id, "scheme": p.scheme, "ip": p.ip, "port": p.port,
                     "auth_masked": _mask_auth(p.auth), "status": p.status,
                     "label": p.label, "provider": p.provider or "(unowned)"}
                    for p in proxies],
                "rate_limits": [
                    {"source": r.source, "max_qps": r.max_qps,
                     "max_concurrent": r.max_concurrent,
                     "throttled": (r.max_qps or 0) > 0}
                    for r in limits],
                "ban_rules": [
                    {"source": r.source, "rule_type": r.rule_type,
                     "pattern": r.pattern[:60], "classification": r.classification,
                     "enabled": r.enabled}
                    for r in rules],
            })
        finally:
            s.close()

    @app.get("/panel/partials/proxy", response_class=HTMLResponse)
    def partial_proxy(request: Request):
        """Polled circuit-health matrix: per-(source, proxy) state from the
        shared cold table (near-realtime — hot state lives in proxy-redis)."""
        try:
            s = _session()
            try:
                rows = (s.query(SourceProxyHealth)
                        .order_by(SourceProxyHealth.source, SourceProxyHealth.proxy_id)
                        .limit(500).all())
            finally:
                s.close()
            return templates.TemplateResponse(
                request, "partial_proxy.html", {"circuits": [h.toDict() for h in rows]})
        except Exception as e:  # noqa: BLE001
            return _unavailable(e)

    @app.post("/panel/proxy/import")
    async def proxy_import(request: Request):
        form = await request.form()
        try:
            out = _proxy_control("POST", "/management/proxies/import", {
                "provider": form.get("provider") or "paid-static",
                "text": form.get("text", ""),
            })
        except RuntimeError as e:
            return _proxy_redirect(err=str(e))
        return _proxy_redirect(msg=(
            f"imported {out.get('imported', 0)}, skipped {out.get('skipped', 0)}"
            + (f", updated {out['updated']}" if out.get("updated") else "")))

    @app.post("/panel/proxy/circuits/reset")
    async def proxy_circuit_reset(request: Request):
        form = await request.form()
        try:
            _proxy_control("POST", "/management/circuits/reset", {
                "source": form.get("source"), "proxy_id": int(form.get("proxy_id", 0)),
                "actor": form.get("actor") or "panel"})
        except (RuntimeError, TypeError, ValueError) as e:
            return _proxy_redirect(err=str(e))
        return _proxy_redirect(msg=f"circuit {form.get('source')}/{form.get('proxy_id')} -> HALF_OPEN")

    @app.post("/panel/proxy/rate-limits/{source}")
    async def proxy_rate_limit(source: str, request: Request):
        form = await request.form()
        raw_qps = (form.get("max_qps") or "").strip()
        try:
            max_qps = float(raw_qps) if raw_qps else 0.0  # blank = no throttling
            _proxy_control("PUT", f"/management/rate-limits/{source}", {
                "max_qps": max_qps,
                "max_concurrent": int(form.get("max_concurrent") or 4)})
        except (RuntimeError, TypeError, ValueError) as e:
            return _proxy_redirect(err=str(e))
        return _proxy_redirect(msg=f"rate limit for {source} saved")

    # NOTE: the dynamic {proxy_id} route goes LAST so the literal paths above
    # are never shadowed by a partial int-converter match.
    @app.post("/panel/proxy/{proxy_id}/status")
    async def proxy_set_status(proxy_id: int, request: Request):
        form = await request.form()
        try:
            out = _proxy_control("POST", f"/management/proxies/{proxy_id}/status", {
                "status": form.get("status"), "actor": form.get("actor") or "panel"})
        except RuntimeError as e:
            return _proxy_redirect(err=str(e))
        if out.get("status") == "not_found":
            return _proxy_redirect(err=f"proxy {proxy_id} not found")
        return _proxy_redirect(msg=f"proxy {proxy_id} -> {out.get('proxy_status')}")

    return app


app = create_app()
